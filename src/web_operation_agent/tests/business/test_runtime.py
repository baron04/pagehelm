"""Business Skill loading and read-only runtime-tool transitions."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from web_operation_agent.answer.answering import StructuredQueryAnswer
from web_operation_agent.business.runtime import (
    BusinessMutationBlockedError, BusinessMutationIndeterminate,
    BusinessSelectionOption, BusinessSkill, BusinessToolContractError,
    BusinessToolError, BusinessToolIdentityError,
    BusinessToolPermissionError, BusinessToolRequestError, BusinessToolResult,
    BusinessToolRuntime, BusinessToolUnavailableError, Citation,
)
from web_operation_agent.composition.ports import ResultPostconditionVerifier
from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.answer.evidence import Origin, QueryEvidence, RecordEvidence
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent.business.mutation import MemoryMutationJournal, MutationJournalError
from web_operation_agent.execution.session import ServerAgentSession, TaskSpec
from web_operation_agent.tests.support.business import (
    SCHEMA, WRITE_SCHEMA, _Browser, _Chat, _Executor, _MutationExecutor,
    _observation, _operation_context, _skill, _write_skill,
)


class SkillCitations(unittest.TestCase):
    """`provenance` is what makes a hand-written playbook checkable, so it has a grammar.

    Before this it was validated as "a non-empty list of strings", and four spellings had
    accumulated across nine shipped artifacts. Nothing could resolve any of them, so a
    citation that had rotted looked exactly like one that never pointed anywhere.
    """

    def _artifact(self, provenance, path: Path) -> Path:
        path.write_text(json.dumps({
            "schema_version": 1, "skill_id": "fixture.companies-query",
            "title": "Query companies", "intent": "Read current companies.",
            "triggers": ["companies"], "instructions": ["Call query_companies."],
            "tool_names": ["query_companies"], "provenance": provenance,
            "read_only": True,
        }) + "\n")
        return path

    def test_a_citation_round_trips_through_its_parts(self):
        for text in ("src:odoo@18.0:addons/sale/views/sale_order_views.xml#action_orders",
                     "src:twenty@v2.39.0:packages/twenty-server/src/x.ts",
                     "api:odoo@18.0:/jsonrpc#object.execute_kw",
                     "api:twenty@v2.39.0:/rest/open-api/core#/paths/~1companies/post"):
            with self.subTest(text=text):
                self.assertEqual(Citation.parse(text).text, text)

    def test_every_spelling_this_repository_used_to_ship_is_now_refused(self):
        """The point of the grammar is that the old entries do not survive it."""
        for legacy in ("odoo:18.0:addons/sale/views/sale_order_views.xml#action_orders",
                       "twenty/v2.39.0:packages/twenty-docs/x.mdx",
                       "minimal-demo://contacts/v1",
                       "Odoo external API: /jsonrpc object.execute_kw",
                       "Twenty self-hosted /rest/companies API"):
            with self.subTest(legacy=legacy):
                with self.assertRaises(ValueError):
                    Citation.parse(legacy)

    def test_a_source_citation_cannot_escape_the_repository(self):
        for escape in ("src:odoo@18.0:/etc/passwd",
                       "src:odoo@18.0:addons/../../etc/passwd"):
            with self.subTest(escape=escape):
                with self.assertRaisesRegex(ValueError, "relative path inside"):
                    Citation.parse(escape)

    def test_an_api_citation_must_name_a_path_on_the_origin(self):
        with self.assertRaisesRegex(ValueError, "path on the application's origin"):
            Citation.parse("api:odoo@18.0:jsonrpc")

    def test_a_skill_whose_provenance_does_not_parse_does_not_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._artifact(["fixture:companies"], Path(tmp) / "skill.json")
            with self.assertRaisesRegex(ValueError, "provenance entry must read"):
                BusinessSkill.load(path)

    def test_a_repeated_citation_does_not_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._artifact(["src:fixture@v1:a.json#x", "src:fixture@v1:a.json#x"],
                                  Path(tmp) / "skill.json")
            with self.assertRaisesRegex(ValueError, "unique"):
                BusinessSkill.load(path)

    def test_every_shipped_skill_artifact_parses(self):
        """The nine artifacts in `examples/` are the thing this grammar had to fit.

        Read as files, never imported: the core suite must still run with no application
        distribution installed, which is the boundary `check_core_boundary.py` guards. From
        an installed wheel there are no examples to read and this has nothing to say.
        """
        root = Path(__file__).resolve().parents[4] / "examples"
        if not root.is_dir():
            self.skipTest("running outside the checkout; no example artifacts to read")
        artifacts = [path for path in root.rglob("skills/*.json")
                     if "/build/" not in str(path)]
        self.assertGreaterEqual(len(artifacts), 9, "the example Skills moved")
        for path in artifacts:
            with self.subTest(path=path.name):
                skill = BusinessSkill.load(path)
                kinds = {Citation.parse(entry).kind for entry in skill.provenance}
                self.assertTrue(kinds <= {"src", "api"})


class BusinessRuntimeValidation(unittest.TestCase):
    def test_complete_ambiguous_navigation_query_becomes_a_server_choice(self):
        class AmbiguousExecutor(_Executor):
            @staticmethod
            def execute(name, args, *, context=None):
                origin = Origin(kind="typed", reference="fixture", observed_at="now",
                                scope="current workspace")
                return BusinessToolResult(
                    content={"companies": [
                        {"name": "Acme", "domain": "acme.test"},
                        {"name": "Azure", "domain": "azure.test"},
                    ]},
                    audit={"application": "fixture", "operation": name,
                           "source_complete": True},
                    query_answer=StructuredQueryAnswer(
                        collection="companies", fields=("name", "domain"),
                        records=(("Acme", "acme.test"), ("Azure", "azure.test")),
                        source="fixture API", scope="current workspace",
                        observed_at="now", source_complete=True,
                        result_truncated=False, matched_count=2,
                        source_total=2, records_scanned=2),
                    evidence=(
                        QueryEvidence(
                            evidence_id="query#1", origin=origin, collection="companies",
                            conditions=("name contains A",), matched_count=2,
                            records_scanned=2, covers_whole_scope=True,
                            looked_for="name contains A"),
                        RecordEvidence(
                            evidence_id="record#1", origin=origin, record_id="company/1",
                            field_name="name", field_meaning="company name",
                            field_was_read=True, value="Acme", value_type="text"),
                        RecordEvidence(
                            evidence_id="record#2", origin=origin, record_id="company/2",
                            field_name="name", field_meaning="company name",
                            field_was_read=True, value="Azure", value_type="text"),
                    ),
                    selection_options=(
                        BusinessSelectionOption("company/1", "Acme — acme.test"),
                        BusinessSelectionOption("company/2", "Azure — azure.test"),
                    ),
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = _skill(root / "query.json")
            runtime = BusinessToolRuntime(
                (skill,), AmbiguousExecutor(),
                route_skill_hints=((r"^/companies$", skill.skill_id),))
            chat = _Chat([("query_companies", {})])
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="打开 A", entry_path="/companies",
                              supported_scope="Read and open current companies."),
                chat=chat, browser=_Browser(), control=ReadOnlyControl(),
                ledger=SessionLedger(root / "session.jsonl", "ambiguous-navigation"),
                business_tools=runtime, deterministic_read_navigation=True,
                allow_clarification=True,
            )
            session.entry_observation = _observation(1)
            session.plan()

            result = session.run()

            self.assertEqual(result.terminal_reason, "needs_input")
            self.assertEqual(result.agent_said, "找到多个匹配项，请选择要打开的记录：")
            self.assertEqual(
                [item["label"] for item in result.clarification["choices"]],
                ["Acme — acme.test", "Azure — azure.test"])
            self.assertEqual(len(chat.messages), 1, "the server must not ask the model to copy choices")
            event = session.ledger.of_kind("clarification_requested")[-1]
            self.assertEqual(event["deterministic"], "complete_navigation_query")

    def test_complete_navigation_query_beyond_the_choice_limit_goes_back_to_the_model(self):
        """A complete read with more matches than one question can offer must not end the turn.

        The server choice is only a shortcut for a set it can show whole. Nine matches cannot
        be one ask_user question, so the model decides how to narrow the request.
        """
        names = [f"A{index}" for index in range(1, 10)]

        class ManyExecutor(_Executor):
            @staticmethod
            def execute(name, args, *, context=None):
                origin = Origin(kind="typed", reference="fixture", observed_at="now",
                                scope="current workspace")
                return BusinessToolResult(
                    content={"companies": [{"name": item} for item in names]},
                    audit={"application": "fixture", "operation": name,
                           "source_complete": True},
                    query_answer=StructuredQueryAnswer(
                        collection="companies", fields=("name",),
                        records=tuple((item,) for item in names),
                        source="fixture API", scope="current workspace",
                        observed_at="now", source_complete=True,
                        result_truncated=False, matched_count=len(names),
                        source_total=len(names), records_scanned=len(names)),
                    evidence=(
                        QueryEvidence(
                            evidence_id="query#1", origin=origin, collection="companies",
                            conditions=("name contains A",), matched_count=len(names),
                            records_scanned=len(names), covers_whole_scope=True,
                            looked_for="name contains A"),
                        *(RecordEvidence(
                            evidence_id=f"record#{index}", origin=origin,
                            record_id=f"company/{index}", field_name="name",
                            field_meaning="company name", field_was_read=True,
                            value=item, value_type="text")
                          for index, item in enumerate(names, 1)),
                    ),
                    selection_options=tuple(
                        BusinessSelectionOption(f"company/{index}", item)
                        for index, item in enumerate(names, 1)),
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = _skill(root / "query.json")
            runtime = BusinessToolRuntime(
                (skill,), ManyExecutor(),
                route_skill_hints=((r"^/companies$", skill.skill_id),))
            chat = _Chat([
                ("query_companies", {}),
                ("ask_user", {"question": "请说明公司名称。", "response_kind": "free_text",
                              "choice_ids": []}),
            ])
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="打开 A", entry_path="/companies",
                              supported_scope="Read and open current companies."),
                chat=chat, browser=_Browser(), control=ReadOnlyControl(),
                ledger=SessionLedger(root / "session.jsonl", "many-navigation"),
                business_tools=runtime, deterministic_read_navigation=True,
                allow_clarification=True,
            )
            session.entry_observation = _observation(1)
            session.plan()

            result = session.run()

            self.assertEqual(result.terminal_reason, "needs_input")
            self.assertEqual(result.clarification["response_kind"], "free_text")
            self.assertEqual(len(chat.messages), 2, "the model must see the complete read")
            self.assertEqual(
                session.ledger.of_kind("clarification_candidates_ready")[-1]["count"],
                len(names))
            self.assertFalse(session.ledger.of_kind("turn_execution_failed"))
            self.assertNotIn(
                "deterministic", session.ledger.of_kind("clarification_requested")[-1])

    def test_a_complete_read_that_looked_for_nothing_is_not_a_choice_question(self):
        """Several records are an ambiguity only when the person's own words matched them.

        "打开金额最高的销售订单" names a rule, not a record. The read it drives carries no
        name, so every order matches, and the server answered with a list of all five and
        asked which one to open -- the choice the person had already made. A read that
        looked for nothing goes back to the model, which can compute the one it names.
        """
        rows = (("Acme", "1000.00"), ("Azure", "287.50"))

        class WholeCollectionExecutor(_Executor):
            @staticmethod
            def execute(name, args, *, context=None):
                origin = Origin(kind="typed", reference="fixture", observed_at="now",
                                scope="current workspace")
                return BusinessToolResult(
                    content={"companies": [{"name": item, "total": total}
                                           for item, total in rows]},
                    audit={"application": "fixture", "operation": name,
                           "source_complete": True},
                    query_answer=StructuredQueryAnswer(
                        collection="companies", fields=("name", "total"),
                        records=rows, source="fixture API", scope="current workspace",
                        observed_at="now", source_complete=True, result_truncated=False,
                        matched_count=len(rows), source_total=len(rows),
                        records_scanned=len(rows)),
                    evidence=(
                        QueryEvidence(
                            evidence_id="query#1", origin=origin, collection="companies",
                            conditions=("no filter",), matched_count=len(rows),
                            records_scanned=len(rows), covers_whole_scope=True),
                        *(RecordEvidence(
                            evidence_id=f"record#{index}", origin=origin,
                            record_id=f"company/{index}", field_name="name",
                            field_meaning="company name", field_was_read=True,
                            value=item, value_type="text")
                          for index, (item, _total) in enumerate(rows, 1)),
                    ),
                    selection_options=tuple(
                        BusinessSelectionOption(f"company/{index}", f"{item} — {total}")
                        for index, (item, total) in enumerate(rows, 1)),
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = _skill(root / "query.json")
            runtime = BusinessToolRuntime(
                (skill,), WholeCollectionExecutor(),
                route_skill_hints=((r"^/companies$", skill.skill_id),))
            chat = _Chat([
                ("query_companies", {}),
                ("ask_user", {"question": "按哪个口径算最高？", "response_kind": "free_text",
                              "choice_ids": []}),
            ])
            session = ServerAgentSession(
                task=TaskSpec(task_ref="T", prompt="打开金额最高的公司",
                              entry_path="/companies",
                              supported_scope="Read and open current companies."),
                chat=chat, browser=_Browser(), control=ReadOnlyControl(),
                ledger=SessionLedger(root / "session.jsonl", "unnamed-navigation"),
                business_tools=runtime, deterministic_read_navigation=True,
                allow_clarification=True,
            )
            session.entry_observation = _observation(1)
            session.plan()

            result = session.run()

            self.assertEqual(result.terminal_reason, "needs_input")
            self.assertEqual(len(chat.messages), 2, "the model must see the complete read")
            self.assertNotIn(
                "deterministic", session.ledger.of_kind("clarification_requested")[-1])
            self.assertNotIn("请选择要打开的记录", result.agent_said)

    def test_server_policy_text_cannot_manufacture_a_skill_match(self):
        """Route the person's words; keep the server's policy out of intent matching.

        This is the exact shape used by the Odoo demo.  Its form-draft Skill has a
        ``do not submit`` trigger while the product scope says ``Do not submit page
        forms``.  Before this regression, that server-authored sentence won over the
        trusted customer-page hint and every entity-only open request loaded the draft
        tool instead of the customer query.
        """
        class CatalogExecutor:
            @staticmethod
            def tool_schemas():
                return (SCHEMA, {"type": "function", "function": {
                    "name": "prepare_company_form", "description": "Prepare a form.",
                    "parameters": {"type": "object", "properties": {},
                                   "additionalProperties": False}}})

            @staticmethod
            def tool_effects():
                return {"query_companies": "read", "prepare_company_form": "read"}

            @staticmethod
            def execute(*_args, **_kwargs):
                raise AssertionError("routing the turn must not dispatch a tool")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            query = _skill(root / "query.json")
            draft_path = root / "draft.json"
            draft_path.write_text(json.dumps({
                "schema_version": 1,
                "skill_id": "fixture.companies-draft",
                "title": "Prepare company form",
                "intent": "Prepare one form without submitting.",
                "triggers": ["do not submit", "不要提交"],
                "instructions": ["Prepare the form and leave submission to the user."],
                "tool_names": ["prepare_company_form"],
                "provenance": ["src:fixture@v1:companies.json#draft"],
                "read_only": True,
            }) + "\n")
            draft = BusinessSkill.load(draft_path)
            runtime = BusinessToolRuntime(
                (query, draft), CatalogExecutor(),
                route_skill_hints=((r"^/companies$", query.skill_id),))
            ledger = SessionLedger(root / "session.jsonl", "policy-routing")

            session = ServerAgentSession(
                task=TaskSpec(
                    task_ref="T", prompt="打开 azur", entry_path="/companies",
                    supported_scope=("Read current records. Do not submit page forms. "
                                     "Open only an exact API route.")),
                chat=_Chat([]), browser=_Browser(), control=ReadOnlyControl(),
                ledger=ledger, business_tools=runtime,
                business_mutation_approval=lambda *_args, **_kwargs: None,
                deterministic_read_navigation=True,
            )

            self.assertEqual(session._task_business_skill_ids, {query.skill_id})
            self.assertEqual(session._auto_loaded_business_skill_ids, {query.skill_id})
            event = ledger.of_kind("business_skill_candidates")[-1]
            self.assertTrue(event["route_context_used"])
            self.assertEqual(event["trigger_matches"], [])
            self.assertEqual(event["task_digest"], hashlib.sha256(
                "打开 azur".encode("utf-8")).hexdigest())

    def test_a_clarification_reply_inherits_only_the_unresolved_user_goal(self):
        class CatalogExecutor:
            @staticmethod
            def tool_schemas():
                return (SCHEMA, WRITE_SCHEMA)

            @staticmethod
            def tool_effects():
                return {"query_companies": "read", "create_company": "write"}

            @staticmethod
            def execute(*_args, **_kwargs):
                raise AssertionError("routing the turn must not dispatch a tool")

        context = {
            "schema_version": 1,
            "prior_turns": [{
                "message_id": "message-1", "user": "打开 A",
                "assistant": "请给出完整名称。", "status": "needs_input",
            }],
            "pending_clarification": {
                "schema_version": 1, "clarification_id": "clarify-1",
                "task_id": "task-1", "question": "请给出完整名称。",
                "response_kind": "free_text", "choices": [],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            query = _skill(root / "query.json")
            create_path = root / "create.json"
            create_path.write_text(json.dumps({
                "schema_version": 1,
                "skill_id": "fixture.companies-create",
                "title": "Create company",
                "intent": "Create one company.",
                "triggers": ["create company", "创建公司"],
                "instructions": ["Query duplicates, then propose one company."],
                "tool_names": ["query_companies", "create_company"],
                "provenance": ["src:fixture@v1:companies.json#create"],
                "read_only": False,
            }) + "\n")
            create = BusinessSkill.load(create_path)
            runtime = BusinessToolRuntime(
                (query, create), CatalogExecutor(),
                route_skill_hints=((r"^/companies$", query.skill_id),))

            continued = ServerAgentSession(
                task=TaskSpec(task_ref="continued", prompt="Azure Interior",
                              entry_path="/companies", supported_scope="read and open"),
                chat=_Chat([]), browser=_Browser(), control=ReadOnlyControl(),
                ledger=SessionLedger(root / "continued.jsonl", "continued"),
                business_tools=runtime,
                business_mutation_approval=lambda *_args, **_kwargs: None,
                deterministic_read_navigation=True,
                conversation_context=context,
            )
            self.assertEqual(continued._task_business_skill_ids, {query.skill_id})
            self.assertEqual(continued._auto_loaded_business_skill_ids, {query.skill_id})
            self.assertTrue(continued._navigation_only)
            routed = continued.ledger.of_kind("business_skill_candidates")[-1]
            self.assertTrue(routed["clarification_context_used"])
            self.assertTrue(routed["route_context_used"])

            replaced = ServerAgentSession(
                task=TaskSpec(task_ref="replaced", prompt="创建公司 Zephyr",
                              entry_path="/companies", supported_scope="create one"),
                chat=_Chat([]), browser=_Browser(), control=ReadOnlyControl(),
                ledger=SessionLedger(root / "replaced.jsonl", "replaced"),
                business_tools=runtime,
                business_mutation_approval=lambda *_args, **_kwargs: None,
                deterministic_read_navigation=True,
                conversation_context=context,
            )
            self.assertEqual(replaced._task_business_skill_ids, {create.skill_id})
            self.assertFalse(replaced._navigation_only)
            rerouted = replaced.ledger.of_kind("business_skill_candidates")[-1]
            self.assertFalse(rerouted.get("clarification_context_used", False))

    def test_read_failures_have_stable_machine_classification(self):
        class FailingExecutor(_Executor):
            @staticmethod
            def execute(*_args, **_kwargs):
                raise OSError("private transport detail")

        class InvalidResultExecutor(_Executor):
            @staticmethod
            def execute(*_args, **_kwargs):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            skill = _skill(Path(tmp) / "query-skill.json")
            loaded = {skill.skill_id}
            runtime = BusinessToolRuntime((skill,), _Executor(), application_id="fixture")
            with self.assertRaises(BusinessToolRequestError) as request:
                runtime.execute("query_companies", {}, set())
            self.assertEqual(request.exception.code, "business_request")
            with self.assertRaises(BusinessToolIdentityError) as identity:
                runtime.execute(
                    "query_companies", {}, loaded,
                    context=_operation_context(application_id="another-app"))
            self.assertEqual(identity.exception.code, "business_identity")
            unavailable_runtime = BusinessToolRuntime((skill,), FailingExecutor())
            with self.assertRaises(BusinessToolUnavailableError) as unavailable:
                unavailable_runtime.execute("query_companies", {}, loaded)
            self.assertEqual(unavailable.exception.code, "business_unavailable")
            contract_runtime = BusinessToolRuntime((skill,), InvalidResultExecutor())
            with self.assertRaises(BusinessToolContractError) as contract:
                contract_runtime.execute("query_companies", {}, loaded)
            self.assertEqual(contract.exception.code, "business_contract")

    def test_bound_tool_requires_application_and_declared_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _skill(Path(tmp) / "query-skill.json")
            runtime = BusinessToolRuntime(
                (skill,), _Executor(), application_id="fixture",
                required_scopes={"query_companies": "companies:read"})
            loaded = {skill.skill_id}
            result = runtime.execute(
                "query_companies", {}, loaded,
                context=_operation_context(scopes=("companies:read",)))
            self.assertEqual(result.content["matched_count"], 1)
            with self.assertRaises(BusinessToolPermissionError) as refused:
                runtime.execute(
                    "query_companies", {}, loaded,
                    context=_operation_context(scopes=("companies:write",)))
            self.assertEqual(refused.exception.required_scope, "companies:read")
            with self.assertRaisesRegex(BusinessToolError, "another application"):
                runtime.execute(
                    "query_companies", {}, loaded,
                    context=_operation_context(application_id="another-app"))

    def test_mutation_proposal_cannot_cross_operation_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            executor = _MutationExecutor()
            runtime = BusinessToolRuntime(
                (skill,), executor, application_id="fixture",
                required_scopes={"create_company": "companies:write"})
            loaded = {skill.skill_id}
            first = _operation_context(operation_id="op-1")
            second = _operation_context(operation_id="op-2")
            proposal = runtime.prepare_mutation(
                "create_company", {"name": "Acme"}, loaded, context=first)
            self.assertEqual(
                proposal.operation_context_digest, first.binding_digest)
            with self.assertRaisesRegex(BusinessToolError, "another operation context"):
                runtime.execute_mutation(
                    proposal, proposal.content_digest, loaded, context=second)
            self.assertEqual(executor.executions, 0)

    def test_skill_can_load_from_bytes_with_a_logical_artifact_uri(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "skill.json"
            from_path = _skill(path)
            raw = path.read_bytes()
            from_bytes = BusinessSkill.from_bytes(
                raw, artifact_uri="memory://fixture/companies-query.json")

            self.assertEqual(from_bytes.loaded_card(), from_path.loaded_card())
            self.assertEqual(from_bytes.artifact_sha256, from_path.artifact_sha256)
            self.assertEqual(
                from_bytes.artifact_uri, "memory://fixture/companies-query.json")
            self.assertEqual(from_bytes.artifact_path, from_bytes.artifact_uri)
            with self.assertRaisesRegex(TypeError, "must be bytes"):
                BusinessSkill.from_bytes(  # type: ignore[arg-type]
                    raw.decode(), artifact_uri="memory://fixture/skill.json")
            with self.assertRaisesRegex(ValueError, "artifact_uri"):
                BusinessSkill.from_bytes(raw, artifact_uri="")

    def test_tool_is_unavailable_until_relevant_skill_is_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _skill(Path(tmp) / "skill.json")
            runtime = BusinessToolRuntime((skill,), _Executor())
            relevant = runtime.relevant("Which companies are in this workspace?")
            self.assertEqual([item.skill_id for item in relevant], [skill.skill_id])
            self.assertEqual(runtime.schemas_for(set()), [])
            with self.assertRaisesRegex(BusinessToolError, "load the matching"):
                runtime.execute("query_companies", {}, set())
            loaded, error = runtime.load(skill.skill_id, {skill.skill_id})
            self.assertEqual(error, "")
            self.assertEqual(loaded, skill)
            self.assertEqual(
                [item["function"]["name"] for item in runtime.schemas_for({skill.skill_id})],
                ["query_companies"])

    def test_empty_trigger_match_uses_a_bounded_read_only_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _skill(Path(tmp) / "skill.json")
            runtime = BusinessToolRuntime((skill,), _Executor())
            selection = runtime.select(
                "Show the current workspace overview", allow_mutations=False)

            self.assertTrue(selection.fallback_used)
            self.assertEqual(selection.trigger_matches, ())
            self.assertEqual(selection.candidate_ids, (skill.skill_id,))

    def test_skill_instruction_cannot_reference_an_unavailable_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "skill.json"
            path.write_text(json.dumps({
                "schema_version": 1,
                "skill_id": "fixture.invalid-tool",
                "title": "Invalid tool reference",
                "intent": "Prove instruction and callable surfaces stay aligned.",
                "triggers": ["invalid"],
                "instructions": ["Call delete_everything before answering."],
                "tool_names": ["query_companies"],
                "provenance": ["src:fixture@v1:invalid.json#invalid"],
                "read_only": True,
            }))
            with self.assertRaisesRegex(ValueError, "unavailable.*delete_everything"):
                BusinessToolRuntime((BusinessSkill.load(path),), _Executor())

    def test_result_rejects_credential_shaped_fields(self):
        with self.assertRaisesRegex(ValueError, "credential-shaped"):
            BusinessToolResult(content={"access_token": "leak"}, audit={})

    def test_result_routes_are_safe_and_must_be_visible_to_the_model(self):
        with self.assertRaisesRegex(ValueError, "safe relative"):
            BusinessToolResult(
                content={"ui_path": "https://other.test/record/1"}, audit={},
                allowed_routes=("https://other.test/record/1",))
        with self.assertRaisesRegex(ValueError, "safe relative"):
            BusinessToolResult(
                content={"ui_path": "/\\other.test/record/1"}, audit={},
                allowed_routes=("/\\other.test/record/1",))
        with self.assertRaisesRegex(ValueError, "must appear"):
            BusinessToolResult(
                content={"companies": []}, audit={},
                allowed_routes=("/object/company/company-1",))

    def test_skill_and_executor_must_declare_the_same_tools(self):
        class EmptyExecutor:
            @staticmethod
            def tool_schemas():
                return ()

            @staticmethod
            def tool_effects():
                return {}

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "same tools"):
                BusinessToolRuntime((_skill(Path(tmp) / "skill.json"),), EmptyExecutor())

    def test_custom_mutation_journal_must_declare_restart_behavior(self):
        class UnclassifiedJournal:
            def register(self, *_args, **_kwargs):
                pass

            def claim(self, *_args, **_kwargs):
                pass

            def finish(self, *_args, **_kwargs):
                pass

            def get(self, *_args, **_kwargs):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(TypeError, "MutationJournalDurability"):
                BusinessToolRuntime(
                    (_skill(Path(tmp) / "skill.json"),), _Executor(),
                    mutation_journal=UnclassifiedJournal())

    def test_read_only_skill_cannot_expose_a_write_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "skill.json"
            data = json.loads(Path(_skill(path).artifact_path).read_text())
            data["tool_names"] = ["create_company"]
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "read-only"):
                BusinessToolRuntime((BusinessSkill.load(path),), _MutationExecutor())

    def test_separate_runtimes_share_normalized_business_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            journal = MemoryMutationJournal()
            executors = [_MutationExecutor(), _MutationExecutor()]
            runtimes = [BusinessToolRuntime((skill,), executor, mutation_journal=journal)
                        for executor in executors]
            proposals = [runtime.prepare_mutation(
                "create_company", {"name": name}, {skill.skill_id})
                for runtime, name in zip(runtimes, (" Acme ", "Acme"))]
            self.assertNotEqual(proposals[0].content_digest, proposals[1].content_digest)
            journal.claim(proposals[0].content_digest, "")
            with self.assertRaisesRegex(BusinessMutationBlockedError, "business key") as blocked:
                runtimes[1].execute_mutation(
                    proposals[1], proposals[1].content_digest, {skill.skill_id})
            self.assertEqual(blocked.exception.reason, "business_key_unresolved")
            self.assertEqual(executors[1].executions, 0)

    def test_mutation_requires_exact_approval_and_is_single_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            executor = _MutationExecutor()
            journal = MemoryMutationJournal()
            runtime = BusinessToolRuntime(
                (skill,), executor, mutation_journal=journal)
            loaded = {skill.skill_id}
            with self.assertRaisesRegex(BusinessToolError, "explicit approval"):
                runtime.execute("create_company", {"name": " Acme "}, loaded)
            proposal = runtime.prepare_mutation(
                "create_company", {"name": " Acme "}, loaded)
            self.assertEqual(proposal.args, {"name": "Acme"})
            with self.assertRaisesRegex(BusinessToolError, "does not match"):
                runtime.execute_mutation(proposal, "0" * 64, loaded)
            self.assertEqual(executor.executions, 0)
            result = runtime.execute_mutation(
                proposal, proposal.content_digest, loaded)
            self.assertTrue(result.content["created"])
            self.assertEqual(executor.executions, 1)
            self.assertEqual(
                journal.get(proposal.content_digest).state, "completed")
            self.assertEqual(
                journal.get(proposal.content_digest).result_digest,
                result.content_digest)
            with self.assertRaisesRegex(BusinessMutationBlockedError, "already completed") as reused:
                runtime.execute_mutation(proposal, proposal.content_digest, loaded)
            self.assertEqual(reused.exception.reason, "already_consumed")
            self.assertEqual(executor.executions, 1)

    def test_indeterminate_mutation_is_consumed_and_cannot_be_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            executor = _MutationExecutor(indeterminate=True)
            journal = MemoryMutationJournal()
            runtime = BusinessToolRuntime(
                (skill,), executor, mutation_journal=journal)
            proposal = runtime.prepare_mutation(
                "create_company", {"name": "Acme"}, {skill.skill_id})
            with self.assertRaises(BusinessMutationIndeterminate):
                runtime.execute_mutation(
                    proposal, proposal.content_digest, {skill.skill_id})
            self.assertEqual(
                journal.get(proposal.content_digest).state, "indeterminate")
            with self.assertRaisesRegex(BusinessToolError, "already indeterminate"):
                runtime.execute_mutation(
                    proposal, proposal.content_digest, {skill.skill_id})
            self.assertEqual(executor.executions, 1)

    def test_unverified_adapter_postcondition_keeps_acknowledged_write_indeterminate(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            executor = _MutationExecutor()
            journal = MemoryMutationJournal()
            runtime = BusinessToolRuntime(
                (skill,), executor, mutation_journal=journal,
                postcondition_verifier=ResultPostconditionVerifier((
                    ("create_company", ("content.readback_verified",)),
                )))
            loaded = {skill.skill_id}
            proposal = runtime.prepare_mutation(
                "create_company", {"name": "Acme"}, loaded)

            with self.assertRaisesRegex(
                    BusinessMutationIndeterminate, "not verified"):
                runtime.execute_mutation(
                    proposal, proposal.content_digest, loaded)

            self.assertEqual(executor.executions, 1)
            self.assertEqual(
                journal.get(proposal.content_digest).state, "indeterminate")

    def test_missing_durable_completion_turns_success_into_indeterminate(self):
        class BrokenFinishJournal(MemoryMutationJournal):
            def finish(self, proposal_digest, state, *, result_digest="", now_ms=None):
                if state == "completed":
                    raise MutationJournalError("disk unavailable")
                return super().finish(
                    proposal_digest, state, result_digest=result_digest,
                    now_ms=now_ms)

        with tempfile.TemporaryDirectory() as tmp:
            skill = _write_skill(Path(tmp) / "write-skill.json")
            executor = _MutationExecutor()
            journal = BrokenFinishJournal()
            runtime = BusinessToolRuntime(
                (skill,), executor, mutation_journal=journal)
            proposal = runtime.prepare_mutation(
                "create_company", {"name": "Acme"}, {skill.skill_id})
            with self.assertRaisesRegex(
                    BusinessMutationIndeterminate, "durable outcome"):
                runtime.execute_mutation(
                    proposal, proposal.content_digest, {skill.skill_id})
            self.assertEqual(executor.executions, 1)
            self.assertEqual(
                journal.get(proposal.content_digest).state, "dispatching")
