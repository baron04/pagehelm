"""Business Skill loading and read-only runtime-tool transitions."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from web_operation_agent.answer.answering import StructuredQueryAnswer
from web_operation_agent.business.runtime import (
    BusinessMutationBlockedError, BusinessMutationIndeterminate,
    BusinessMutationRefusedError, BusinessSkill, BusinessToolDispatchError, BusinessToolError,
    BusinessToolIdentityError,
    BusinessToolPermissionError, BusinessToolResult,
    BusinessToolRuntime, BusinessToolUnavailableError,
)
from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.answer.evidence import Origin, QueryEvidence
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent.state.identity import OperationContext, PrincipalContext
from web_operation_agent.business.mutation import MemoryMutationJournal
from web_operation_agent.execution.session import (CAPABILITY_GAP_TERMINAL,
                                        ServerAgentSession, TaskSpec)
from web_operation_agent.execution.knowledge_tools import register_capability_evidence
from web_operation_agent.tests.support.business import (
    SCHEMA, WRITE_SCHEMA, _Browser, _Chat, _Executor, _MutationExecutor,
    _ProjectedExecutor, _observation, _operation_context, _skill, _write_skill,
)


class BusinessReActTransition(unittest.TestCase):
    def test_skill_free_paraphrase_cannot_become_a_false_capability_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            context = _operation_context()
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "lexical-gap")
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="创建一个公司。",
                              supported_scope="Create one company."),
                chat=_Chat([]), browser=_Browser(), control=ReadOnlyControl(),
                ledger=ledger, business_tools=BusinessToolRuntime(
                    (), _MutationExecutor()),
                business_mutation_approval=lambda proposal: None,
                principal=context.principal, session_id=context.session_id,
                turn_id=context.turn_id,
                disabled_actions=frozenset({"click", "fill", "select", "submit", "back"}))

            self.assertEqual(register_capability_evidence(session), "")
            self.assertEqual(len(ledger.of_kind("capability_check_inconclusive")), 1)
            self.assertFalse(session.evidence.ids())

    def test_a_capability_question_cannot_turn_into_a_business_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _skill(Path(tmp) / "skill.json")
            runtime = BusinessToolRuntime((skill,), _Executor())
            chat = _Chat([("finish", {"done": True,
                                      "why": "我可以查询当前公司。"})])
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="你可以做什么？",
                              supported_scope="Read current records."),
                chat=chat, browser=_Browser(), control=ReadOnlyControl(),
                ledger=SessionLedger(Path(tmp) / "session.jsonl", "capabilities"),
                business_tools=runtime, step_budget=2, wall_budget_s=30)
            session.entry_observation = _observation(1)
            session.plan()

            result = session.run()

            self.assertEqual(result.agent_said, "我可以查询当前公司。")
            self.assertEqual(chat.tool_names, [{"finish"}])
            self.assertIn("Query companies", chat.messages[0][1]["content"])

    def test_assistant_history_does_not_change_the_user_text_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _skill(Path(tmp) / "skill.json")
            runtime = BusinessToolRuntime((skill,), _Executor())
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "assistant-history")
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Open the second one.",
                              supported_scope="Read the current page only."),
                chat=_Chat([]), browser=_Browser(),
                control=ReadOnlyControl(),
                ledger=ledger,
                business_tools=runtime,
                conversation_context={
                    "schema_version": 1,
                    "prior_turns": [{
                        "message_id": "m1",
                        "user": "Show the available records.",
                        "assistant": "The companies include Acme and Example.",
                        "status": "finished",
                    }],
                },
            )

            self.assertEqual(session._task_business_skill_ids, {skill.skill_id})
            event = ledger.of_kind("business_skill_candidates")[-1]
            self.assertTrue(event["fallback_used"])
            self.assertFalse(event["prior_user_context_used"])
            self.assertNotIn("companies include", json.dumps(event))

    def test_unrelated_followup_does_not_use_prior_text_beyond_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _skill(Path(tmp) / "skill.json")
            runtime = BusinessToolRuntime((skill,), _Executor())
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "unrelated-followup")
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Show the dashboard.",
                              supported_scope="Read the current page only."),
                chat=_Chat([]), browser=_Browser(),
                control=ReadOnlyControl(),
                ledger=ledger,
                business_tools=runtime,
                conversation_context={
                    "schema_version": 1,
                    "prior_turns": [{
                        "message_id": "m1",
                        "user": "Which companies are available?",
                        "assistant": "Acme",
                        "status": "finished",
                    }],
                },
            )

            self.assertEqual(session._task_business_skill_ids, {skill.skill_id})
            event = ledger.of_kind("business_skill_candidates")[-1]
            self.assertTrue(event["fallback_used"])
            self.assertFalse(event["prior_user_context_used"])

            opening = session._opening(_observation(1))
            self.assertNotIn("PRIOR CONVERSATION CONTEXT", opening)
            self.assertFalse(session._conversation_context_selected)

    def test_referential_intent_selects_bounded_conversation_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "referential-context")
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Open the second one.",
                              supported_scope="Read the current page only."),
                chat=_Chat([]), browser=_Browser(), control=ReadOnlyControl(),
                ledger=ledger,
                conversation_context={
                    "schema_version": 1,
                    "prior_turns": [{
                        "message_id": "m1", "user": "Show the records.",
                        "assistant": "First and Second.", "status": "finished",
                    }],
                },
            )

            opening = session._opening(_observation(1))
            self.assertIn("PRIOR CONVERSATION CONTEXT", opening)
            self.assertIn("First and Second", opening)
            self.assertTrue(session._conversation_context_selected)

    def test_denied_server_mutation_is_terminal_and_never_executes(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            executor = _MutationExecutor()
            runtime = BusinessToolRuntime((skill,), executor)
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("create_company", {"name": "Acme"}),
            ])
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "write-denied")

            def deny(proposal):
                return SimpleNamespace(
                    approval_id="approval-1", decision="denied",
                    plan_digest=proposal.content_digest)

            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Create company Acme.",
                              supported_scope="Create exactly one company."),
                chat=chat, browser=_Browser(), control=ReadOnlyControl(),
                ledger=ledger,
                step_budget=3, wall_budget_s=30, run_id="write-denied",
                business_tools=runtime, business_mutation_approval=deny)
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "approval_denied")
            self.assertEqual(executor.executions, 0)
            self.assertEqual(result.business_control["approval_denied"], 1)
            self.assertEqual(result.business_control["mutation_execution_started"], 0)
            self.assertEqual(len(ledger.of_kind("business_mutation_denied")), 1)

    def test_in_flight_business_key_is_a_product_refusal_before_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            journal = MemoryMutationJournal()
            first = BusinessToolRuntime(
                (skill,), _MutationExecutor(), mutation_journal=journal)
            blocker = first.prepare_mutation(
                "create_company", {"name": "Acme"}, {skill.skill_id})
            journal.claim(blocker.content_digest, "")

            executor = _MutationExecutor()
            runtime = BusinessToolRuntime(
                (skill,), executor, mutation_journal=journal)
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("create_company", {"name": " Acme "}),
            ])
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "write-blocked")

            def approve(proposal):
                return SimpleNamespace(
                    approval_id="approval-2", decision="approved",
                    plan_digest=proposal.content_digest)

            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Create company Acme.",
                              supported_scope="Create exactly one company."),
                chat=chat, browser=_Browser(), control=ReadOnlyControl(),
                ledger=ledger, step_budget=3, wall_budget_s=30,
                run_id="write-blocked", business_tools=runtime,
                business_mutation_approval=approve)
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "business_mutation_blocked")
            self.assertEqual(executor.executions, 0)
            self.assertEqual(result.business_control["approval_granted"], 1)
            self.assertEqual(result.business_control["mutation_execution_started"], 0)
            self.assertEqual(len(ledger.of_kind("business_mutation_execution_started")), 0)
            event = ledger.of_kind("business_mutation_blocked")[-1]
            self.assertEqual(event["reason"], "business_key_unresolved")
            self.assertFalse(event["dispatched"])
            self.assertFalse(event["side_effect_possible"])

    def test_authoritative_business_refusal_is_terminal_before_approval(self):
        class DuplicateRefusingExecutor(_MutationExecutor):
            @staticmethod
            def prepare_mutation(name, args, *, context=None):
                raise BusinessMutationRefusedError(
                    "a company with the same name already exists", reason="duplicate")

        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            executor = DuplicateRefusingExecutor()
            runtime = BusinessToolRuntime((skill,), executor)
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("create_company", {"name": "Acme"}),
            ])
            asked = []
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "duplicate-refused")
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="创建公司 Acme。",
                              supported_scope="Create exactly one company."),
                chat=chat, browser=_Browser(), control=ReadOnlyControl(),
                ledger=ledger, step_budget=3, wall_budget_s=30,
                run_id="duplicate-refused", business_tools=runtime,
                business_mutation_approval=lambda proposal: asked.append(proposal))
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "business_mutation_refused")
            self.assertEqual(result.detail, "duplicate")
            self.assertEqual(executor.executions, 0)
            self.assertEqual(asked, [])
            self.assertEqual(result.business_control["approval_requested"], 0)
            event = ledger.of_kind("business_mutation_rejected")[-1]
            self.assertEqual(event["error_code"], "business_mutation_refused")
            self.assertEqual(event["reason"], "duplicate")
            self.assertFalse(event["dispatched"])
            self.assertFalse(event["side_effect_possible"])

    def test_a_missing_scope_is_a_typed_permission_refusal_not_an_identity_fault(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            runtime = BusinessToolRuntime(
                (skill,), _MutationExecutor(), application_id="fixture",
                required_scopes={"create_company": "companies:write"})
            with self.assertRaises(BusinessToolPermissionError) as refused:
                runtime.prepare_mutation(
                    "create_company", {"name": "Acme"}, {skill.skill_id},
                    context=_operation_context(scopes=("companies:read",)))
            self.assertEqual(refused.exception.code, "business_permission_denied")
            self.assertEqual(refused.exception.layer, "tool_authorization")
            self.assertEqual(refused.exception.required_scope, "companies:write")
            # A principal for another application is still an identity fault, told apart
            # by its type rather than by what its message says.
            with self.assertRaises(BusinessToolIdentityError) as identity:
                runtime.prepare_mutation(
                    "create_company", {"name": "Acme"}, {skill.skill_id},
                    context=_operation_context(application_id="another-app"))
            self.assertNotIsInstance(identity.exception, BusinessToolPermissionError)

    def _principal_bound_session(self, tmp, runtime, chat, context, approve, run_id):
        session = ServerAgentSession(
            task=TaskSpec(task_ref="T", prompt="Create company Acme.",
                          supported_scope="Create exactly one company."),
            chat=chat, browser=_Browser(), control=ReadOnlyControl(),
            ledger=SessionLedger(Path(tmp) / "session.jsonl", run_id),
            step_budget=5, wall_budget_s=30, run_id=run_id, business_tools=runtime,
            business_mutation_approval=approve, principal=context.principal,
            session_id=context.session_id, turn_id=context.turn_id)
        session.entry_observation = _observation(1)
        session.plan()
        return session

    def test_a_capability_this_account_was_not_granted_says_so(self):
        """A gap the account caused reads differently from one the deployment caused (CR-14).

        A reader asking for a change got "当前会话不支持这项操作" -- which reads as though a
        different session would support it, so the person reopens the assistant instead of
        asking whoever grants permissions. The catalog holds the capability; this delegation
        may not use it, and the sentence now says that.
        """
        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            runtime = BusinessToolRuntime((skill,), _MutationExecutor(),
                                          application_id="fixture")
            chat = _Chat([
                ("check_capability", {}),
                ("finish", {"outcome": "unable", "answer": {
                    "version": 1, "coverage": "undetermined", "conclusions": [
                        {"kind": "capability_gap", "evidence_ref": "capability#1"}]}}),
            ])
            # No approval path and no page actions that could make the change: this is what
            # a reader's delegation looks like from inside the session.
            context = _operation_context(scopes=("companies:read",))
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Create company Acme.",
                              supported_scope="Read current companies."),
                chat=chat, browser=_Browser(), control=ReadOnlyControl(),
                ledger=SessionLedger(Path(tmp) / "session.jsonl", "capability-withheld"),
                step_budget=5, wall_budget_s=30, run_id="capability-withheld",
                business_tools=runtime, business_mutation_approval=None,
                principal=context.principal, session_id=context.session_id,
                turn_id=context.turn_id,
                disabled_actions=frozenset({"click", "fill", "select", "submit", "back"}))
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, CAPABILITY_GAP_TERMINAL)
            self.assertIn("does not have permission", result.agent_said)
            self.assertIn("Ask an administrator", result.agent_said)
            self.assertNotIn("this session", result.agent_said)
            event = session.ledger.of_kind("capability_check")[-1]
            self.assertEqual(event["matches"], [])
            self.assertEqual(event["withheld"], ["create_company"])

    def test_a_tool_authorization_refusal_ends_the_turn_before_a_proposal_or_page_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            executor = _MutationExecutor()
            runtime = BusinessToolRuntime(
                (skill,), executor, application_id="fixture",
                required_scopes={"create_company": "companies:write"})
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("create_company", {"name": "Acme"}),
                ("click", {"ref": "e1"}),
            ])
            asked = []
            session = self._principal_bound_session(
                tmp, runtime, chat, _operation_context(scopes=("companies:read",)),
                lambda proposal, **_: asked.append(proposal), "write-forbidden")
            browser = session.browser
            result = session.run()

            self.assertEqual(result.terminal_reason, "permission_denied")
            self.assertEqual(executor.executions, 0)
            self.assertEqual(asked, [])
            self.assertEqual(len(chat.messages), 2, "the model is not asked again")
            self.assertEqual(browser.actions, [])
            self.assertEqual(result.business_control["approval_requested"], 0)
            ledger = session.ledger
            self.assertEqual(ledger.of_kind("business_mutation_proposed"), [])
            event = ledger.of_kind("business_mutation_permission_denied")[-1]
            self.assertEqual(event["layer"], "tool_authorization")
            self.assertEqual(event["required_scope"], "companies:write")
            self.assertEqual(event["error_code"], "business_permission_denied")
            self.assertFalse(event["dispatched"])
            self.assertFalse(event["side_effect_possible"])

    def test_an_application_refusal_after_dispatch_is_definite_not_indeterminate(self):
        class RefusingExecutor(_MutationExecutor):
            def execute_mutation(self, name, args, *, context=None):
                self.executions += 1
                raise BusinessToolPermissionError("the application refused this actor")

        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            executor = RefusingExecutor()
            journal = MemoryMutationJournal()
            runtime = BusinessToolRuntime(
                (skill,), executor, application_id="fixture", mutation_journal=journal,
                required_scopes={"create_company": "companies:write"})
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("create_company", {"name": "Acme"}),
            ])

            def approve(proposal, *, context=None):
                return SimpleNamespace(
                    approval_id="approval-1", decision="approved",
                    plan_digest=proposal.content_digest,
                    operation_context_digest=context.binding_digest)

            session = self._principal_bound_session(
                tmp, runtime, chat, _operation_context(), approve, "write-refused")
            result = session.run()

            self.assertEqual(result.terminal_reason, "permission_denied")
            self.assertEqual(executor.executions, 1)
            self.assertEqual(result.business_control["mutation_indeterminate"], 0)
            ledger = session.ledger
            self.assertEqual(ledger.of_kind("business_mutation_indeterminate"), [])
            event = ledger.of_kind("business_mutation_permission_denied")[-1]
            self.assertEqual(event["layer"], "application")
            self.assertEqual(event["approval_id"], "approval-1")
            self.assertTrue(event["dispatched"])
            self.assertFalse(event["side_effect_possible"])
            digest = ledger.of_kind("business_mutation_proposed")[-1]["proposal"]["digest"]
            self.assertEqual(journal.get(digest).state, "failed")

    def test_a_transport_failure_after_mutation_dispatch_stays_indeterminate(self):
        class LostAcknowledgement(_MutationExecutor):
            def execute_mutation(self, name, args, *, context=None):
                self.executions += 1
                raise BusinessToolDispatchError("upstream response was lost")

        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            journal = MemoryMutationJournal()
            runtime = BusinessToolRuntime(
                (skill,), LostAcknowledgement(), application_id="fixture",
                mutation_journal=journal,
                required_scopes={"create_company": "companies:write"})
            context = _operation_context()
            proposal = runtime.prepare_mutation(
                "create_company", {"name": "Acme"}, {skill.skill_id}, context=context)

            with self.assertRaises(BusinessMutationIndeterminate) as raised:
                runtime.execute_mutation(
                    proposal, proposal.content_digest, {skill.skill_id}, context=context)

            self.assertEqual(raised.exception.phase, "dispatch")
            intent = journal.get(proposal.content_digest)
            self.assertEqual(intent.state, "indeterminate")
            self.assertEqual(
                json.loads(intent.reconciliation_json)["phase"], "dispatch")

    def test_a_refused_read_is_handed_back_and_never_opens_the_page_as_its_source(self):
        """The refusal reaches the model; what it may answer from does not grow (D13).

        Ending the turn threw away the part of a question this account may see. The refusal
        now comes back, and with nothing read the turn still ends on it -- what must not
        happen, at either layer, is the same facts being taken off the page instead.
        """

        class RefusingReads(_Executor):
            @staticmethod
            def execute(*_args, **_kwargs):
                raise BusinessToolPermissionError("the application refused this read")

        cases = (
            ("tool_authorization", _Executor(), ("companies:write",), False),
            ("application", RefusingReads(), ("companies:read",), True),
        )
        for layer, executor, scopes, dispatched in cases:
            with self.subTest(layer), tempfile.TemporaryDirectory() as tmp:
                skill = _skill(Path(tmp) / "query-skill.json")
                runtime = BusinessToolRuntime(
                    (skill,), executor, application_id="fixture",
                    required_scopes={"query_companies": "companies:read"})
                chat = _Chat([
                    ("load_skill", {"skill_id": skill.skill_id}),
                    ("query_companies", {}),
                    # The page is where the same facts would be, and the turn ends before
                    # the model can put them in an answer.
                    ("finish", {"done": True, "why": "Read the companies."}),
                ])
                session = self._principal_bound_session(
                    tmp, runtime, chat, _operation_context(scopes=scopes),
                    lambda proposal, **_: None, f"read-refused-{layer}")
                browser = session.browser
                result = session.run()

                self.assertEqual(result.terminal_reason, "permission_denied")
                self.assertEqual(len(chat.messages), 3, "the refusal is handed back once")
                self.assertEqual(browser.actions, [])
                ledger = session.ledger
                self.assertEqual(ledger.of_kind("business_tool_rejected"), [])
                self.assertEqual(ledger.of_kind("business_tool_result"), [])
                event = ledger.of_kind("business_tool_permission_denied")[-1]
                self.assertTrue(event["turn_continued"])
                self.assertEqual(event["layer"], layer)
                self.assertEqual(event["dispatched"], dispatched)
                self.assertFalse(event["side_effect_possible"])
                self.assertEqual(event["error_code"], "business_permission_denied")
                if layer == "tool_authorization":
                    self.assertEqual(event["required_scope"], "companies:read")

    def test_an_unresolved_write_in_another_workspace_does_not_block_this_one(self):
        def context_in(workspace, operation_id):
            now = int(time.time() * 1000)
            principal = PrincipalContext(
                1, "fixture", "tenant-1", workspace, "actor-1", "delegation-1",
                now - 1_000, now + 60_000, ("companies:write",))
            return OperationContext.create(
                principal, session_id="session-1", turn_id="message-1",
                operation_id=operation_id, deadline_ms=now + 30_000, now_ms=now)

        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            journal = MemoryMutationJournal()
            runtime = BusinessToolRuntime(
                (skill,), _MutationExecutor(), application_id="fixture",
                mutation_journal=journal,
                required_scopes={"create_company": "companies:write"})
            loaded = {skill.skill_id}
            elsewhere = context_in("workspace-2", "op-elsewhere")
            blocker = runtime.prepare_mutation(
                "create_company", {"name": "Acme"}, loaded, context=elsewhere)
            journal.claim(blocker.content_digest, blocker.operation_context_digest)

            here = context_in("workspace-1", "op-here")
            proposal = runtime.prepare_mutation(
                "create_company", {"name": "Acme"}, loaded, context=here)
            created = runtime.execute_mutation(
                proposal, proposal.content_digest, loaded, context=here)
            self.assertTrue(created.content["created"])

            # The reverse check: the same name in the blocker's own workspace is blocked.
            again = context_in("workspace-2", "op-again")
            repeat = runtime.prepare_mutation(
                "create_company", {"name": "Acme"}, loaded, context=again)
            with self.assertRaises(BusinessMutationBlockedError):
                runtime.execute_mutation(
                    repeat, repeat.content_digest, loaded, context=again)

    def test_cancellation_after_approval_never_dispatches_the_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            executor = _MutationExecutor()
            runtime = BusinessToolRuntime((skill,), executor)
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("create_company", {"name": "Acme"}),
            ])
            cancelled = {"value": False}

            def approve_then_cancel(proposal):
                cancelled["value"] = True
                return SimpleNamespace(
                    approval_id="approval-cancel", decision="approved",
                    plan_digest=proposal.content_digest)

            ledger = SessionLedger(Path(tmp) / "session.jsonl", "write-cancelled")
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Create company Acme.",
                              supported_scope="Create exactly one company."),
                chat=chat, browser=_Browser(), control=ReadOnlyControl(),
                ledger=ledger,
                step_budget=3, wall_budget_s=30, run_id="write-cancelled",
                business_tools=runtime, business_mutation_approval=approve_then_cancel,
                cancelled=lambda: cancelled["value"])
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "user_cancelled")
            self.assertEqual(executor.executions, 0)
            self.assertEqual(result.business_control["approval_granted"], 0)
            event = ledger.of_kind("business_mutation_cancelled_before_dispatch")[-1]
            self.assertFalse(event["dispatched"])
            self.assertFalse(event["side_effect_possible"])

    def test_cancellation_during_read_tool_dispatches_no_browser_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            cancelled = {"value": False}

            class CancellingExecutor(_Executor):
                @staticmethod
                def execute(name, args):
                    result = _Executor.execute(name, args)
                    cancelled["value"] = True
                    return result

            skill = _skill(Path(tmp) / "query-skill.json")
            browser = _Browser()
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="List companies",
                              supported_scope="Read current companies."),
                chat=_Chat([
                    ("load_skill", {"skill_id": skill.skill_id}),
                    ("query_companies", {}),
                ]), browser=browser, control=ReadOnlyControl(),
                ledger=SessionLedger(Path(tmp) / "session.jsonl", "read-cancelled"),
                step_budget=3,
                wall_budget_s=30, run_id="read-cancelled",
                business_tools=BusinessToolRuntime((skill,), CancellingExecutor()),
                cancelled=lambda: cancelled["value"])
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "user_cancelled")
            self.assertEqual(browser.actions, [])

    def test_approved_server_mutation_returns_route_to_react_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            executor = _MutationExecutor()
            runtime = BusinessToolRuntime((skill,), executor)
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("create_company", {"name": "Acme"}),
                ("goto", {"url": "/object/company/company-2"}),
                (None, {"content": "Created and opened Acme."}),
            ])
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "write-approved")

            def approve(proposal):
                return SimpleNamespace(
                    approval_id="approval-1", decision="approved",
                    plan_digest=proposal.content_digest)

            browser = _Browser()
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Create company Acme.",
                              supported_scope="Create exactly one company."),
                chat=chat, browser=browser, control=ReadOnlyControl(),
                ledger=ledger,
                step_budget=4, wall_budget_s=30, run_id="write-approved",
                business_tools=runtime, business_mutation_approval=approve,
                plain_business_answer_min_page_actions=1)
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "agent_finished")
            self.assertEqual(executor.executions, 1)
            self.assertEqual(result.business_control["mutation_completed"], 1)
            self.assertEqual(browser.url, "http://app.test/object/company/company-2")
            self.assertEqual(len(ledger.of_kind("business_mutation_result")), 1)

    def test_registered_write_without_any_skill_still_requires_approval_and_once_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = _MutationExecutor()
            runtime = BusinessToolRuntime((), executor)
            chat = _Chat([
                ("create_company", {"name": "Acme"}),
                ("goto", {"url": "/object/company/company-2"}),
                (None, {"content": "Created and opened Acme."}),
            ])
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "write-no-skill")
            approvals = []

            def approve(proposal):
                approvals.append(proposal)
                return SimpleNamespace(
                    approval_id="approval-1", decision="approved",
                    plan_digest=proposal.content_digest)

            browser = _Browser()
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Create company Acme.",
                              supported_scope="Create exactly one company."),
                chat=chat, browser=browser, control=ReadOnlyControl(),
                ledger=ledger, step_budget=4, wall_budget_s=30,
                business_tools=runtime, business_mutation_approval=approve,
                plain_business_answer_min_page_actions=1)
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(session._task_business_skills, ())
            self.assertIn("create_company", chat.tool_names[0])
            self.assertEqual(result.terminal_reason, "agent_finished")
            self.assertEqual(len(approvals), 1)
            self.assertEqual(executor.executions, 1)
            self.assertEqual(result.business_control["mutation_completed"], 1)
            self.assertEqual(len(ledger.of_kind("business_mutation_result")), 1)
            self.assertEqual(browser.url, "http://app.test/object/company/company-2")

    def test_a_write_the_request_does_not_ask_for_is_never_proposed(self):
        """A read request cannot become a write, whatever a record or the model says.

        Typed tools no longer wait on a Skill, so the request itself is what can ask for a
        change: the write is not offered, and a call to it is refused before any proposal,
        so no approval card is ever put in front of the person. The turn goes on.
        """
        for prompt, asked in (("List the companies.", False),
                              ("Which companies were created this week?", False),
                              ("Create company Acme.", True),
                              ("Create Acme at acme.example.", True)):
            with self.subTest(prompt), tempfile.TemporaryDirectory() as tmp:
                executor = _MutationExecutor()
                runtime = BusinessToolRuntime((), executor)
                self.assertEqual(runtime.requested_writes(prompt),
                                 frozenset({"create_company"} if asked else ()))
                if asked:
                    continue
                chat = _Chat([
                    ("create_company", {"name": "Mallory"}),
                    ("finish", {"outcome": "unable"}),
                ])
                ledger = SessionLedger(Path(tmp) / "session.jsonl", "read-no-write")
                approvals = []
                session = ServerAgentSession(
                    task=TaskSpec(task_ref="T", prompt=prompt,
                                  supported_scope="Read companies."),
                    chat=chat, browser=_Browser(), control=ReadOnlyControl(),
                    ledger=ledger, step_budget=4, wall_budget_s=30,
                    business_tools=runtime,
                    business_mutation_approval=lambda proposal: approvals.append(proposal),
                    plain_business_answer_min_page_actions=1)
                session.entry_observation = _observation(1)
                session.plan()
                session.run()

                self.assertNotIn("create_company", chat.tool_names[0])
                self.assertEqual(approvals, [])
                self.assertEqual(executor.executions, 0)
                self.assertEqual(ledger.of_kind("business_mutation_proposed"), [])
                refused = ledger.of_kind("business_mutation_rejected")
                self.assertEqual([row["error_code"] for row in refused],
                                 ["business_write_not_requested"])
                self.assertFalse(refused[0]["dispatched"])
                self.assertEqual(len(chat.tool_names), 2, "the refusal comes back to the model")

    def test_a_reply_to_the_assistant_s_question_carries_the_write_it_answers(self):
        """"Acme Ltd" asks for nothing by itself; as the answer to the question a create
        request raised, it continues that request, and its write stays asked for."""
        pending = {"clarification_id": "c1", "question": "Which name?"}
        for context, asked in (
                ({"schema_version": 1, "pending_clarification": pending,
                  "prior_turns": [{"message_id": "m1", "user": "创建公司",
                                   "assistant": "Which name?", "status": "needs_input"}]},
                 frozenset({"create_company"})),
                (None, frozenset())):
            with self.subTest(continued=context is not None), \
                    tempfile.TemporaryDirectory() as tmp:
                runtime = BusinessToolRuntime((_write_skill(Path(tmp) / "write.json"),),
                                              _MutationExecutor())
                session = ServerAgentSession(
                    task=TaskSpec(task_ref="T", prompt="Acme Ltd",
                                  supported_scope="Create exactly one company."),
                    chat=_Chat([]), browser=_Browser(), control=ReadOnlyControl(),
                    ledger=SessionLedger(Path(tmp) / "session.jsonl", "continued-write"),
                    business_tools=runtime, conversation_context=context,
                    business_mutation_approval=lambda proposal: None)
                self.assertEqual(session._requested_writes, asked)

    def test_cancelling_followup_observation_does_not_rewrite_completed_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            cancelled = {"value": False}

            class CancelAfterCompletedMutation(_MutationExecutor):
                def execute_mutation(self, name, args, *, context=None):
                    result = super().execute_mutation(name, args, context=context)
                    cancelled["value"] = True
                    return result

            skill = _write_skill(Path(tmp) / "write-skill.json")
            executor = CancelAfterCompletedMutation()
            journal = MemoryMutationJournal()
            runtime = BusinessToolRuntime(
                (skill,), executor, mutation_journal=journal)
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("create_company", {"name": "Acme"}),
            ])
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "write-followup-cancelled")

            def approve(proposal):
                return SimpleNamespace(
                    approval_id="approval-1", decision="approved",
                    plan_digest=proposal.content_digest)

            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Create company Acme.",
                              supported_scope="Create exactly one company."),
                chat=chat, browser=_Browser(), control=ReadOnlyControl(),
                ledger=ledger,
                step_budget=3, wall_budget_s=30, run_id="write-followup-cancelled",
                business_tools=runtime, business_mutation_approval=approve,
                cancelled=lambda: cancelled["value"])
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            completed = ledger.of_kind("business_mutation_result")[-1]
            intent = journal.get(completed["proposal_digest"])
            self.assertIsNotNone(intent)
            self.assertEqual(intent.state, "completed")
            self.assertEqual(intent.result_digest, completed["result_digest"])
            self.assertEqual(result.terminal_reason, "user_cancelled")
            self.assertIn("completed", result.agent_said)
            self.assertEqual(executor.executions, 1)
            self.assertEqual(result.business_control["mutation_completed"], 1)
            self.assertEqual(result.business_control["mutation_indeterminate"], 0)
            followup = ledger.of_kind("business_mutation_followup_cancelled")
            self.assertEqual(len(followup), 1)
            self.assertTrue(followup[0]["completed"])

    def test_indeterminate_server_mutation_stops_without_model_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            executor = _MutationExecutor(indeterminate=True)
            runtime = BusinessToolRuntime((skill,), executor)
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("create_company", {"name": "Acme"}),
                ("create_company", {"name": "Acme"}),
            ])
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "write-unknown")

            def approve(proposal):
                return SimpleNamespace(
                    approval_id="approval-1", decision="approved",
                    plan_digest=proposal.content_digest)

            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Create company Acme.",
                              supported_scope="Create exactly one company."),
                chat=chat, browser=_Browser(), control=ReadOnlyControl(),
                ledger=ledger,
                step_budget=4, wall_budget_s=30, run_id="write-unknown",
                business_tools=runtime, business_mutation_approval=approve)
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "indeterminate_write")
            self.assertEqual(result.steps_used, 2)
            self.assertEqual(executor.executions, 1)
            self.assertEqual(result.business_control["mutation_indeterminate"], 1)
            self.assertEqual(len(chat.messages), 2,
                             "the model must not receive a chance to retry")
            self.assertEqual(len(ledger.of_kind("business_mutation_indeterminate")), 1)

    def test_load_query_and_finish_use_server_facts_without_page_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _skill(Path(tmp) / "skill.json")
            runtime = BusinessToolRuntime((skill,), _ProjectedExecutor())
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("query_companies", {}),
                ("finish", {"done": True, "why": "Acme is the current company."}),
            ])
            browser = _Browser()
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "business-react")
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Which companies are in this workspace?",
                              supported_scope="Read current business data only."),
                chat=chat, browser=browser, control=ReadOnlyControl(),
                ledger=ledger,
                step_budget=4, wall_budget_s=30, run_id="business-react",
                business_tools=runtime,
                plain_business_answer_min_page_actions=0)
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "agent_finished")
            self.assertTrue(result.business_answer_projected)
            self.assertEqual(result.steps_used, 3)
            self.assertIn("load_skill", chat.tool_names[0])
            self.assertIn("query_companies", chat.tool_names[0])
            self.assertIn("wait_for", chat.tool_names[0])
            self.assertIn("query_companies", chat.tool_names[1])
            self.assertEqual(browser.actions, [])
            self.assertEqual(ledger.of_kind("action_receipt"), [])
            facts = [message["content"] for turn in chat.messages for message in turn
                     if message.get("role") == "tool"]
            self.assertTrue(any("BUSINESS SKILL LOADED" in text for text in facts))
            self.assertTrue(any('"name":"Acme"' in text for text in facts))
            self.assertTrue(any("SERVER QUERY ANSWER READY" in text for text in facts))
            self.assertEqual(len(ledger.of_kind("business_skill_loaded")), 1)
            self.assertEqual(ledger.of_kind("business_answer")[0]["terminal_tool"], "finish")
            candidates = ledger.of_kind("business_skill_candidates")
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["matcher_version"],
                             "business-skill-trigger-v2")
            self.assertFalse(candidates[0]["fallback_used"])
            self.assertEqual(candidates[0]["candidate_ids"], [skill.skill_id])
            self.assertNotIn("Which companies", json.dumps(candidates[0]))
            rows = ledger.of_kind("business_tool_result")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["audit"]["oauth_actor"], "delegated_user")
            self.assertNotIn("Acme", json.dumps(rows[0]),
                             "the tool audit stores a digest and projection, not raw rows")

    def test_plain_answer_after_successful_read_tool_is_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _skill(Path(tmp) / "skill.json")
            runtime = BusinessToolRuntime((skill,), _ProjectedExecutor())
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("query_companies", {}),
                (None, {"content": "Acme (acme.test)"}),
            ])
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "plain-business-answer")
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Which companies are in this workspace?",
                              supported_scope="Read current business data only."),
                chat=chat, browser=_Browser(), control=ReadOnlyControl(),
                ledger=ledger,
                step_budget=3, wall_budget_s=30, run_id="plain-business-answer",
                business_tools=runtime,
                plain_business_answer_min_page_actions=0)
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "agent_finished")
            self.assertEqual(result.agent_said, "Acme (acme.test)")
            self.assertTrue(result.business_answer_projected)
            self.assertEqual(session.latest_business_tool_result.content["companies"], [{
                "name": "Acme", "domain": "acme.test",
                "ui_path": "/object/company/company-1",
            }])
            self.assertNotIn("latest_business_tool_result", result.to_json(),
                             "bounded data stays in server memory unless the product renders it")
            self.assertEqual(result.steps_used, 3)
            self.assertEqual(result.spend["provider_calls"], 0)
            self.assertEqual(len(ledger.of_kind("business_answer")), 1)
            self.assertEqual(len(ledger.of_kind("no_tool_call")), 0)

    def test_complete_empty_query_does_not_claim_the_requested_record_is_absent(self):
        class EmptyProjectedExecutor(_Executor):
            @staticmethod
            def execute(name, args, *, context=None):
                observed_at = "2026-09-13T04:00:00+00:00"
                origin = Origin(
                    kind="typed", reference="query_companies#0",
                    observed_at=observed_at,
                    scope="companies visible to the delegated user; name contains S00042")
                return BusinessToolResult(
                    content={"companies": [], "matched_count": 0},
                    audit={"application": "fixture", "operation": name,
                           "returned_count": 0},
                    query_answer=StructuredQueryAnswer(
                        collection="companies", fields=("name", "domain"), records=(),
                        source="fixture API", scope=origin.scope, observed_at=observed_at,
                        source_complete=True, result_truncated=False, matched_count=0,
                        source_total=0, records_scanned=0),
                    evidence=(QueryEvidence(
                        evidence_id="query#1", origin=origin, collection="companies",
                        conditions=("name contains S00042",), matched_count=0,
                        records_scanned=0, covers_whole_scope=True),),
                )

        with tempfile.TemporaryDirectory() as tmp:
            skill = _skill(Path(tmp) / "skill.json")
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("query_companies", {}),
                ("finish", {"outcome": "unable"}),
            ])
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "empty-query")
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="报价单 S00042 是哪个客户的？",
                              supported_scope="Read current business data only."),
                chat=chat, browser=_Browser(), control=ReadOnlyControl(), ledger=ledger,
                step_budget=5, wall_budget_s=30, run_id="empty-query",
                business_tools=BusinessToolRuntime((skill,), EmptyProjectedExecutor()),
                plain_business_answer_min_page_actions=0)
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "agent_refused")
            self.assertEqual(result.steps_used, 3)
            self.assertEqual(len(chat.messages), 3)
            self.assertIn("EMPTY QUERY SCOPE", chat.messages[2][-1]["content"])
            self.assertNotIn("未找到", result.agent_said)
            self.assertIsNone(result.verified_answer)
            self.assertFalse(any(event.get("deterministic") == "empty_query"
                                 for event in ledger.of_kind("answer_verified")))

    def test_complete_empty_preflight_does_not_finish_a_mutation_task(self):
        class EmptyMutationExecutor(_MutationExecutor):
            @staticmethod
            def tool_schemas():
                return (SCHEMA, WRITE_SCHEMA)

            @staticmethod
            def tool_effects():
                return {"query_companies": "read", "create_company": "write"}

            @staticmethod
            def execute(name, args, *, context=None):
                if name != "query_companies" or args:
                    raise BusinessToolError("unexpected read")
                observed_at = "2026-09-13T04:00:00+00:00"
                origin = Origin(
                    kind="typed", reference="query_companies#0",
                    observed_at=observed_at, scope="current companies")
                return BusinessToolResult(
                    content={"companies": [], "matched_count": 0},
                    audit={"application": "fixture", "operation": name,
                           "returned_count": 0},
                    query_answer=StructuredQueryAnswer(
                        collection="companies", fields=("name", "domain"), records=(),
                        source="fixture API", scope=origin.scope, observed_at=observed_at,
                        source_complete=True, result_truncated=False, matched_count=0,
                        source_total=0, records_scanned=0),
                    evidence=(QueryEvidence(
                        evidence_id="query#1", origin=origin, collection="companies",
                        conditions=("name contains P72 Acceptance Write",),
                        matched_count=0, records_scanned=0, covers_whole_scope=True),),
                )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "create-skill.json"
            path.write_text(json.dumps({
                "schema_version": 1,
                "skill_id": "fixture.companies-create",
                "title": "Create company",
                "intent": "Create one company after checking duplicates.",
                "triggers": ["create company", "创建公司"],
                "instructions": ["Query duplicates, then propose the write."],
                "tool_names": ["query_companies", "create_company"],
                "provenance": ["src:fixture@v1:companies.json#create"],
                "read_only": False,
            }) + "\n")
            skill = BusinessSkill.load(path)
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("query_companies", {}),
                ("finish", {"outcome": "answered", "answer": {
                    "version": 1, "coverage": "complete",
                    "conclusions": [{"kind": "not_found",
                                     "evidence_ref": "call2-query#1"}],
                }}),
                ("create_company", {"name": "P72 Acceptance Write"}),
            ])
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "empty-preflight")

            def deny(proposal):
                return SimpleNamespace(
                    approval_id="approval-1", decision="denied",
                    plan_digest=proposal.content_digest)

            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="创建公司 P72 Acceptance Write",
                              supported_scope="Create exactly one company."),
                chat=chat, browser=_Browser(), control=ReadOnlyControl(), ledger=ledger,
                step_budget=5, wall_budget_s=30, run_id="empty-preflight",
                business_tools=BusinessToolRuntime(
                    (skill,), EmptyMutationExecutor(),
                    mutation_journal=MemoryMutationJournal()),
                business_mutation_approval=deny,
                plain_business_answer_min_page_actions=0)
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "approval_denied")
            self.assertEqual(result.steps_used, 4)
            self.assertEqual(len(chat.messages), 4)
            self.assertEqual(len(ledger.of_kind("answer_contract_refused")), 1)
            self.assertEqual(ledger.of_kind("answer_verified"), [])

    def test_read_failure_records_its_machine_code_for_external_scoring(self):
        class UnavailableExecutor(_Executor):
            @staticmethod
            def execute(*_args, **_kwargs):
                raise BusinessToolUnavailableError("private upstream detail")

        with tempfile.TemporaryDirectory() as tmp:
            skill = _skill(Path(tmp) / "skill.json")
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "typed-read-failure")
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="List companies",
                              supported_scope="Read current companies."),
                chat=_Chat([
                    ("load_skill", {"skill_id": skill.skill_id}),
                    ("query_companies", {}),
                    ("finish", {"done": False, "why": "The data source failed."}),
                ]), browser=_Browser(), control=ReadOnlyControl(), ledger=ledger,
                step_budget=4,
                wall_budget_s=30, run_id="typed-read-failure",
                business_tools=BusinessToolRuntime((skill,), UnavailableExecutor()))
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "agent_refused")
            rejected = ledger.of_kind("business_tool_rejected")
            self.assertEqual(len(rejected), 1)
            self.assertEqual(rejected[0]["error_code"], "business_unavailable")

    def test_a_typed_mutation_preflight_cannot_be_replaced_by_navigation(self):
        class RoutedMutationExecutor(_MutationExecutor):
            @staticmethod
            def tool_schemas():
                return (SCHEMA, WRITE_SCHEMA)

            @staticmethod
            def tool_effects():
                return {"query_companies": "read", "create_company": "write"}

            @staticmethod
            def execute(name, args, *, context=None):
                return _ProjectedExecutor.execute(name, args, context=context)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "create-skill.json"
            path.write_text(json.dumps({
                "schema_version": 1,
                "skill_id": "fixture.companies-create",
                "title": "Create company",
                "intent": "Create one company after checking duplicates.",
                "triggers": ["create company", "创建公司"],
                "instructions": ["Query, then call create_company."],
                "tool_names": ["query_companies", "create_company"],
                "provenance": ["src:fixture@v1:companies.json#create"],
                "read_only": False,
            }) + "\n")
            skill = BusinessSkill.load(path)
            executor = RoutedMutationExecutor()
            runtime = BusinessToolRuntime(
                (skill,), executor, mutation_journal=MemoryMutationJournal())
            browser = _Browser()
            ledger = SessionLedger(root / "session.jsonl", "mutation-before-route")

            def deny(proposal):
                return SimpleNamespace(
                    approval_id="approval-1", decision="denied",
                    plan_digest=proposal.content_digest)

            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Create company Acme.",
                              supported_scope="Create exactly one company."),
                chat=_Chat([
                    ("load_skill", {"skill_id": skill.skill_id}),
                    ("query_companies", {}),
                    ("goto", {"url": "/object/company/company-1"}),
                    ("create_company", {"name": "Acme"}),
                ]),
                browser=browser, control=ReadOnlyControl(), ledger=ledger,
                step_budget=5, wall_budget_s=30, run_id="mutation-before-route",
                business_tools=runtime, business_mutation_approval=deny,
                operation_confirmation=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("leave to work the page must not be requested")))
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "approval_denied")
            self.assertEqual(browser.actions, [])
            self.assertEqual(
                len(ledger.of_kind("explicit_mutation_navigation_rejected")), 1)

    def test_exact_tool_result_route_can_open_a_record_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _skill(Path(tmp) / "skill.json")
            runtime = BusinessToolRuntime((skill,), _ProjectedExecutor())
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("query_companies", {}),
                ("goto", {"url": "/object/company/company-1"}),
                (None, {"content": "Opened Acme."}),
            ])
            browser = _Browser()
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "business-route")
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Open Acme from companies.",
                              supported_scope="Read and open one company record."),
                chat=chat, browser=browser, control=ReadOnlyControl(),
                ledger=ledger,
                step_budget=4, wall_budget_s=30, run_id="business-route",
                business_tools=runtime,
                plain_business_answer_min_page_actions=1)
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "agent_finished")
            self.assertEqual(result.agent_said, "Opened Acme.")
            self.assertEqual(result.page_actions, 1)
            self.assertFalse(result.business_answer_projected)
            self.assertEqual(browser.actions, ["goto"])
            self.assertEqual(browser.url, "http://app.test/object/company/company-1")
            opened = ledger.of_kind("business_route_opened")
            self.assertEqual(len(opened), 1)
            self.assertNotIn("company-1", json.dumps(opened[0]))

    def test_unreturned_business_route_is_not_opened(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _skill(Path(tmp) / "skill.json")
            runtime = BusinessToolRuntime((skill,), _Executor())
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("query_companies", {}),
                ("goto", {"url": "/object/company/invented"}),
                ("finish", {"done": False, "why": "No verified route."}),
            ])
            browser = _Browser()
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "route-refusal")
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Open the requested item from companies.",
                              supported_scope="Read and open one company record."),
                chat=chat, browser=browser, control=ReadOnlyControl(),
                ledger=ledger,
                step_budget=4, wall_budget_s=30, run_id="route-refusal",
                business_tools=runtime,
                plain_business_answer_min_page_actions=1)
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "agent_refused")
            self.assertEqual(browser.actions, [])
            self.assertEqual(len(ledger.of_kind("page_route_rejected")), 1)
            self.assertEqual(len(ledger.of_kind("business_route_opened")), 0)

    def test_plain_answer_waits_for_required_page_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _skill(Path(tmp) / "skill.json")
            runtime = BusinessToolRuntime((skill,), _Executor())
            chat = _Chat([
                ("load_skill", {"skill_id": skill.skill_id}),
                ("query_companies", {}),
                (None, {"content": "Opened Acme."}),
                ("goto", {"url": "/object/company/company-1"}),
                (None, {"content": "Opened Acme."}),
            ])
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "route-before-answer")
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="Open Acme from companies.",
                              supported_scope="Read and open one company record."),
                chat=chat, browser=_Browser(), control=ReadOnlyControl(),
                ledger=ledger,
                step_budget=5, wall_budget_s=30, run_id="route-before-answer",
                business_tools=runtime,
                plain_business_answer_min_page_actions=1)
            session.entry_observation = _observation(1)
            session.plan()
            result = session.run()

            self.assertEqual(result.terminal_reason, "agent_finished")
            self.assertEqual(result.page_actions, 1)
            self.assertEqual(len(ledger.of_kind("no_tool_call")), 1)
            self.assertEqual(len(ledger.of_kind("business_answer")), 1)


class TriggersRankTheCatalogRatherThanGateIt(unittest.TestCase):
    """A write phrase loses to word order; the read phrase beside it does not.

    A write trigger is a verb and its object -- "confirm quotation", "确认报价单" -- and the
    read trigger for the same subject is usually the object alone. So every sentence matching
    the write also matches the read, and none of the reverse. When word order breaks the write
    phrase, the read Skill still matches; the selection is not empty, so the empty-match
    fallback never runs, and the turn silently loses its write tool. It then refuses for want
    of a tool, which reads exactly like the product declining to act.
    """

    @staticmethod
    def _catalog(root: Path) -> tuple:
        read = root / "read.json"
        read.write_text(json.dumps({
            "schema_version": 1, "skill_id": "fixture.orders-query",
            "title": "Query orders", "intent": "Read current orders.",
            "triggers": ["order", "订单"],
            "instructions": ["Call query_orders and trust its current result."],
            "tool_names": ["query_orders"],
            "provenance": ["src:fixture@v1:orders.json#orders"], "read_only": True,
        }) + "\n")
        # The shape the rule keys on: a mutation Skill carries the read tool its own
        # preflight needs, so its tool set strictly extends the read-only Skill's.
        write = root / "write.json"
        write.write_text(json.dumps({
            "schema_version": 1, "skill_id": "fixture.orders-confirm",
            "title": "Confirm an order", "intent": "Confirm one current order.",
            "triggers": ["confirm order", "确认订单"],
            "instructions": ["Read the order, then propose confirming exactly it."],
            "tool_names": ["query_orders", "confirm_order"],
            "provenance": ["src:fixture@v1:orders.json#confirm"], "read_only": False,
        }) + "\n")
        unrelated = root / "unrelated.json"
        unrelated.write_text(json.dumps({
            "schema_version": 1, "skill_id": "fixture.people-create",
            "title": "Create a person", "intent": "Create one current person.",
            "triggers": ["create person", "创建联系人"],
            "instructions": ["Propose one person and wait for approval."],
            "tool_names": ["create_person"],
            "provenance": ["src:fixture@v1:people.json#create"], "read_only": False,
        }) + "\n")
        return tuple(BusinessSkill.load(path) for path in (read, write, unrelated))

    class _Executor:
        @staticmethod
        def tool_schemas():
            return tuple(
                {"type": "function",
                 "function": {"name": name, "description": name,
                              "parameters": {"type": "object", "properties": {},
                                             "additionalProperties": False}}}
                for name in ("query_orders", "confirm_order", "create_person"))

        @staticmethod
        def tool_effects():
            return {"query_orders": "read", "confirm_order": "write",
                    "create_person": "write"}

    def _select(self, task: str, *, allow_mutations: bool):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = BusinessToolRuntime(self._catalog(Path(tmp)), self._Executor())
            return runtime.select(task, allow_mutations=allow_mutations)

    def test_the_write_skill_that_extends_a_matched_read_skill_is_offered_with_it(self):
        """The verb is last, so only the read phrase matches -- and the write is the point."""
        selection = self._select("把订单 S00002 确认掉", allow_mutations=True)

        self.assertTrue(selection.mutation_surface_widened)
        self.assertEqual(set(selection.candidate_ids),
                         {"fixture.orders-query", "fixture.orders-confirm"})

    def test_an_unrelated_write_skill_is_not_dragged_in(self):
        """Widening keyed on intent would be another guess; this one is keyed on tool sets."""
        selection = self._select("把订单 S00002 确认掉", allow_mutations=True)
        self.assertNotIn("fixture.people-create", selection.candidate_ids)

    def test_a_read_only_turn_is_left_exactly_as_it_was(self):
        selection = self._select("把订单 S00002 确认掉", allow_mutations=False)

        self.assertFalse(selection.mutation_surface_widened)
        self.assertEqual(selection.candidate_ids, ("fixture.orders-query",))

    def test_a_phrase_that_matches_the_write_directly_widens_nothing(self):
        selection = self._select("确认订单 S00002", allow_mutations=True)

        self.assertFalse(selection.mutation_surface_widened)
        self.assertEqual(selection.candidate_ids, ("fixture.orders-confirm",))


    def test_a_sentence_about_two_subjects_gains_neither_ones_write(self):
        """Subsuming every matched read Skill is what keeps a mention from arming a write.

        "Create a person named Bob at Acme company" matches people and companies both, and
        company creation extends only the companies read -- so the sentence is about more
        than that write, and the surface stays where the triggers put it. An earlier adapter
        has carried this as a packaged-Skill test; it belongs next to the rule as well.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orders, confirm, _people = self._catalog(root)
            other = root / "other.json"
            other.write_text(json.dumps({
                "schema_version": 1, "skill_id": "fixture.people-query",
                "title": "Query people", "intent": "Read current people.",
                "triggers": ["person", "联系人"],
                "instructions": ["Call query_people and trust its current result."],
                "tool_names": ["query_people"],
                "provenance": ["src:fixture@v1:people.json#people"], "read_only": True,
            }) + "\n")

            class _Both:
                @staticmethod
                def tool_schemas():
                    return tuple(
                        {"type": "function",
                         "function": {"name": name, "description": name,
                                      "parameters": {"type": "object", "properties": {},
                                                     "additionalProperties": False}}}
                        for name in ("query_orders", "confirm_order", "query_people"))

                @staticmethod
                def tool_effects():
                    return {"query_orders": "read", "confirm_order": "write",
                            "query_people": "read"}

            runtime = BusinessToolRuntime(
                (orders, confirm, BusinessSkill.load(other)), _Both())
            selection = runtime.select("把这个联系人的订单找出来", allow_mutations=True)

        self.assertFalse(selection.mutation_surface_widened)
        self.assertNotIn("fixture.orders-confirm", selection.candidate_ids)

    def test_a_subject_with_no_write_skill_gains_nothing(self):
        """A read Skill no mutation Skill extends adds nothing, however the turn is armed."""
        class _ReadOnly:
            @staticmethod
            def tool_schemas():
                return ({"type": "function",
                         "function": {"name": "query_orders", "description": "query_orders",
                                      "parameters": {"type": "object", "properties": {},
                                                     "additionalProperties": False}}},)

            @staticmethod
            def tool_effects():
                return {"query_orders": "read"}

        with tempfile.TemporaryDirectory() as tmp:
            runtime = BusinessToolRuntime(self._catalog(Path(tmp))[:1], _ReadOnly())
            selection = runtime.select("列出所有订单", allow_mutations=True)

        self.assertFalse(selection.mutation_surface_widened)
        self.assertEqual(selection.candidate_ids, ("fixture.orders-query",))



if __name__ == "__main__":
    unittest.main()
