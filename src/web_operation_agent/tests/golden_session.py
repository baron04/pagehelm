"""Scenarios for the `ServerAgentSession._run` behaviour gold standard.

The recorded snapshot and the sensitivity proof both live in `test_session_golden.py`; this
module only builds the runs. It is separate so the scenario list can be read as one page:
each entry is a composition of the session plus the script the model, the browser and the
control plane follow, and nothing here asserts anything.

**What a scenario records.** `capture()` in the test module takes, for each scenario:

* the exact tool surface offered at every model call;
* a digest and length of the system prompt, and the verbatim text of every tool result --
  that text *is* what the agent was told happened, which is the behaviour under change
  control;
* the browser commands actually dispatched;
* the full audit ledger, normalised;
* the terminal `RunResult`.

**Why these scenarios.** `_run` is 932 lines whose branches are: the six dispatch families
(browser action, business tool, Skill routing, source lookup, application entry, finish),
the four terminations that are not the agent's decision (budgets, cancellation, control
plane), and the write settlement paths (permitted, refused, approved, denied,
indeterminate). One scenario per branch, chosen so that a change to any of them moves a
recorded byte.

Determinism is a precondition, not a convenience: every run is given an explicit `run_id`,
the doubles are scripted, and the capture drops wall-clock fields. A scenario that cannot be
made deterministic does not belong here, because a gold standard that is red at random
teaches a team to re-record without reading.
"""
from __future__ import annotations

import json
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from web_operation_agent.answering import StructuredQueryAnswer
from web_operation_agent.application import SourceCapability
from web_operation_agent.business import (BusinessMutationIndeterminate,
                                          BusinessMutationSpec, BusinessSkill,
                                          BusinessToolError, BusinessToolResult,
                                          BusinessToolRuntime)
from web_operation_agent.controlplane import ControlPlaneError
from web_operation_agent.embedded_agent import ReadOnlyControl
from web_operation_agent.ledger import SessionLedger
from web_operation_agent.schemas import (SCHEMA_VERSIONS, ActionReceipt, ElementRef,
                                         PageObservation)
from web_operation_agent.session import (LabApparatus, ServerAgentSession,
                                         TaskSpec)
from web_operation_agent.tests.chat_port_contract import CancellableChatDouble
from web_operation_agent.tests.knowledge_fixture import (FixtureKnowledgeRouter,
                                                         FixtureSkill)
from web_operation_agent.tests.route_fixture import FIXTURE_ROUTE_PORT, fixture_route
from web_operation_agent.tests.source_fixture import fixture_source_map


# ---------------------------------------------------------------------- doubles


class ScriptedChat(CancellableChatDouble):
    """Replay a fixed script and keep everything the session handed the provider."""

    def __init__(self, script):
        self.script = list(script)
        self.offered_tools: list[list[str]] = []
        self.seen: list[list[dict]] = []

        class _Meter:
            @staticmethod
            def report():
                return {"provider_calls": 0, "cost_cny": 0.0}

        self.meter = _Meter()

    def complete(self, messages, tools, run_id, max_tokens=None, options=None,
                 timeout_s=None):
        self.seen.append([dict(message) for message in messages])
        self.offered_tools.append(sorted(tool["function"]["name"] for tool in tools))
        if not self.script:
            return {"tool_calls": [{"id": f"t{len(self.seen)}", "function": {
                "name": "finish",
                "arguments": '{"done": false, "why": "the script ran out"}'}}]}
        name, args = self.script.pop(0)
        if name is None:
            return {"content": args}
        return {"tool_calls": [{"id": f"t{len(self.seen)}", "function": {
            "name": name, "arguments": json.dumps(args, sort_keys=True)}}]}


class ExplodingChat(ScriptedChat):
    def complete(self, messages, tools, run_id, **kwargs):
        self.seen.append([dict(message) for message in messages])
        self.offered_tools.append(sorted(tool["function"]["name"] for tool in tools))
        raise RuntimeError("the provider raised an unexpected defect")


def observation(epoch: int, url: str = "http://app.test/glasshouse/do/main",
                *, links: tuple[tuple[str, str, str], ...] = ()) -> PageObservation:
    elements = [ElementRef(ref, "link", label, "a", epoch, {"href": href})
                for ref, label, href in links]
    return PageObservation(
        schema_version=SCHEMA_VERSIONS["PageObservation"], epoch=epoch, url=url,
        title="Glasshouse", landmarks=["Main"], elements=elements,
        content=f"page {epoch}")


class ScriptedBrowser:
    """A browser whose URL follows `goto` and whose epoch advances on every command."""

    def __init__(self, *, start: PageObservation | None = None,
                 links: tuple[tuple[str, str, str], ...] = ()):
        self.links = links
        self.start = start or observation(1, links=links)
        self.url = self.start.url
        self.epoch = self.start.epoch
        self.commands: list[tuple[str, dict, bool]] = []

    def observe(self) -> PageObservation:
        return observation(self.epoch, self.url, links=self.links)

    def execute(self, action, args=None, expect_navigation=False) -> ActionReceipt:
        args = dict(args or {})
        self.commands.append((action, args, bool(expect_navigation)))
        before = self.url
        if action == "goto":
            target = str(args.get("url") or "")
            self.url = target if target.startswith("http") else "http://app.test" + target
        self.epoch += 1
        return ActionReceipt(
            schema_version=SCHEMA_VERSIONS["ActionReceipt"],
            command_id=f"c{len(self.commands)}", action=action, args_echo=args, ok=True,
            url_before=before, url_after=self.url, navigated=self.url != before,
            epoch_before=self.epoch - 1, epoch_after=self.epoch,
            observation=observation(self.epoch, self.url, links=self.links))

    @staticmethod
    def console():
        return []

    def close(self):
        pass


class ScriptedControl:
    """One scripted decision window per read; `since` is what `_run` polls."""

    def __init__(self, windows, *, unreadable_after: int | None = None):
        self.windows = [list(window) for window in windows]
        self.unreadable_after = unreadable_after
        self.reads = 0
        self.begun: list[str] = []
        self.ended: list[str] = []
        self._events: list[dict] = []

    def since(self, seen):
        self.reads += 1
        if self.unreadable_after is not None and self.reads > self.unreadable_after:
            raise ControlPlaneError("the gateway is unreachable")
        self._events += self.windows.pop(0) if self.windows else []
        return self._events[seen:], len(self._events)

    def outcome(self):
        from web_operation_agent.controlplane import ControlPlaneClient
        out = ControlPlaneClient.outcome_of(self._events)
        return {"permit_consumed": out["permitted"], "write_blocked": out["refused"],
                "consumed_requests": out["permitted_requests"],
                "blocked_requests": out["refused_requests"],
                "trace_events": len(self._events)}

    def begin_run(self, run_id):
        self.begun.append(run_id)

    def end_run(self, run_id):
        self.ended.append(run_id)


BLOCKED_WRITE = [{"event": "write_blocked", "webapp": "glasshouse", "uri": "sproutSeedling"}]
PERMITTED_WRITE = [{"event": "permit_consumed", "webapp": "glasshouse",
                    "uri": "sproutSeedling"}]


# ---------------------------------------------------------- business tool doubles


QUERY_SCHEMA = {"type": "function", "function": {
    "name": "query_trays", "description": "Read the current seed trays.",
    "parameters": {"type": "object", "properties": {
        "name_contains": {"type": "string"}}, "additionalProperties": False}}}
CREATE_SCHEMA = {"type": "function", "function": {
    "name": "create_tray", "description": "Create exactly one seed tray.",
    "parameters": {"type": "object", "properties": {"name": {"type": "string"}},
                   "required": ["name"], "additionalProperties": False}}}


class QueryExecutor:
    """A read-only executor that answers with a complete, projectable result."""

    def __init__(self, *, projected: bool = True):
        self.projected = projected
        self.calls: list[tuple[str, dict]] = []

    @staticmethod
    def tool_schemas():
        return (QUERY_SCHEMA,)

    @staticmethod
    def tool_effects():
        return {"query_trays": "read"}

    def execute(self, name, args, *, context=None):
        self.calls.append((name, dict(args)))
        if name != "query_trays":
            raise BusinessToolError("unexpected read tool")
        content = {
            "trays": [{"name": "Tray A", "ui_path": "/glasshouse/do/tray?id=1"}],
            "matched_count": 1, "source_total": 1, "scope": "current_delegated_scope",
            "observed_at": "2026-09-08T05:00:00+00:00",
            "completeness": {"source_total_known": True, "source_complete": True,
                             "result_truncated": False},
        }
        answer = None
        if self.projected:
            answer = StructuredQueryAnswer(
                collection="trays", fields=("name",), records=(("Tray A",),),
                source="fixture API", scope="current_delegated_scope",
                observed_at="2026-09-08T05:00:00+00:00", source_complete=True,
                result_truncated=False, matched_count=1, source_total=1, records_scanned=1)
        return BusinessToolResult(
            content=content,
            audit={"application": "fixture", "operation": name, "returned_count": 1,
                   "scope": "current_delegated_scope", "source_complete": True},
            allowed_routes=("/glasshouse/do/tray?id=1",), query_answer=answer)


class MutationExecutor(QueryExecutor):
    """Adds one write tool whose acknowledgement can be made to disappear."""

    def __init__(self, *, indeterminate: bool = False):
        super().__init__(projected=False)
        self.indeterminate = indeterminate
        self.executions = 0

    @staticmethod
    def tool_schemas():
        return (QUERY_SCHEMA, CREATE_SCHEMA)

    @staticmethod
    def tool_effects():
        return {"query_trays": "read", "create_tray": "write"}

    @staticmethod
    def prepare_mutation(name, args, *, context=None):
        if name != "create_tray" or set(args) != {"name"}:
            raise BusinessToolError("invalid create request")
        cleaned = str(args["name"]).strip()
        return BusinessMutationSpec(args={"name": cleaned},
                                    effect="Create one seed tray", targets=(cleaned,))

    def execute_mutation(self, name, args, *, context=None):
        self.executions += 1
        if self.indeterminate:
            raise BusinessMutationIndeterminate("the fixture acknowledgement was lost")
        return BusinessToolResult(
            content={"created": True, "name": args["name"],
                     "ui_path": "/glasshouse/do/tray?id=2"},
            audit={"operation": name, "records_created": 1},
            allowed_routes=("/glasshouse/do/tray?id=2",))


def write_skill_artifact(path: Path) -> BusinessSkill:
    path.write_text(json.dumps({
        "schema_version": 1,
        "skill_id": "fixture.trays-create",
        "title": "Create a seed tray",
        "intent": "Create exactly one seed tray after explicit confirmation.",
        "triggers": ["create tray", "new tray"],
        "instructions": ["Propose one tray and wait for the user's confirmation."],
        "tool_names": ["query_trays", "create_tray"],
        "provenance": ["src:fixture@v1:trays.json#create"],
        "read_only": False,
    }, sort_keys=True) + "\n")
    return BusinessSkill.load(path)


def read_skill_artifact(path: Path) -> BusinessSkill:
    path.write_text(json.dumps({
        "schema_version": 1,
        "skill_id": "fixture.trays-query",
        "title": "Query seed trays",
        "intent": "Read the seed trays the signed-in user can see.",
        "triggers": ["tray", "trays"],
        "instructions": ["Call query_trays and trust only its current result."],
        "tool_names": ["query_trays"],
        "provenance": ["src:fixture@v1:trays.json#trays"],
        "read_only": True,
    }, sort_keys=True) + "\n")
    return BusinessSkill.load(path)


def approve(proposal):
    return SimpleNamespace(approval_id="approval-1", decision="approved",
                           plan_digest=proposal.content_digest)


def deny(proposal):
    return SimpleNamespace(approval_id="approval-1", decision="denied",
                           plan_digest=proposal.content_digest)


TRAY_SKILL = FixtureSkill(
    skill_id="fixture.open-trays", title="Open the seed trays",
    intent="Open the seed tray list.", triggers=("tray", "trays"),
    current_webapp="glasshouse", request="findTrays",
    expected_text=("Seed Trays",), provenance=("fixture:menu:trays",))


# ------------------------------------------------------------------- scenarios


@dataclass(frozen=True)
class Scenario:
    """One named run plus the doubles whose state is part of the recorded behaviour."""

    name: str
    why: str
    build: Callable[[Path], "Prepared"]


@dataclass
class Prepared:
    session: ServerAgentSession
    chat: ScriptedChat
    browser: ScriptedBrowser | None
    extra: Callable[[], dict] | None = None


def _session(tmp: Path, name: str, *, chat, browser, control=None, **overrides):
    arguments = {
        "task": TaskSpec(task_ref="T", prompt="Show the seed trays.",
                         supported_scope="Read the current application only."),
        "chat": chat, "browser": browser, "lab": LabApparatus(arm="G"),
        "control": control if control is not None else ScriptedControl([[], [], [], []]),
        "ledger": SessionLedger(tmp / f"{name}.jsonl", name),
        "run_id": name, "step_budget": 6, "wall_budget_s": 120,
        "application_routes": FIXTURE_ROUTE_PORT,
    }
    arguments.update(overrides)
    session = ServerAgentSession(**arguments)
    session.entry_observation = browser.observe()
    return session


def _plain(tmp: Path, name: str, script, **overrides) -> Prepared:
    chat = ScriptedChat(script)
    browser = ScriptedBrowser()
    session = _session(tmp, name, chat=chat, browser=browser, **overrides)
    return Prepared(session=session, chat=chat, browser=browser)


def refusal(tmp: Path) -> Prepared:
    return _plain(tmp, "refusal",
                  [("finish", {"done": False, "why": "this task is not supported here"})])


def read_and_finish(tmp: Path) -> Prepared:
    return _plain(tmp, "read_and_finish", [
        ("observe", {}),
        ("finish", {"done": True, "why": "the page already shows the trays"}),
    ])


def page_action_then_finish(tmp: Path) -> Prepared:
    trays = fixture_route("glasshouse", "findTrays")
    chat = ScriptedChat([
        ("goto", {"url": trays}),
        ("wait_for", {"text": "page"}),
        ("finish", {"done": True, "why": "the tray list is open"}),
    ])
    browser = ScriptedBrowser(links=(("e1", "Seed Trays", trays),))
    session = _session(tmp, "page_action_then_finish", chat=chat, browser=browser)
    return Prepared(session=session, chat=chat, browser=browser)


def disabled_action_is_refused_before_dispatch(tmp: Path) -> Prepared:
    return _plain(tmp, "disabled_action_is_refused_before_dispatch", [
        ("click", {"ref": "e1"}),
        ("finish", {"done": False, "why": "clicking is not permitted in this run"}),
    ], disabled_actions=frozenset({"click"}), policy_block_limit=3)


def repeated_policy_blocks_end_the_run(tmp: Path) -> Prepared:
    return _plain(tmp, "repeated_policy_blocks_end_the_run", [
        ("click", {"ref": "e1"}), ("click", {"ref": "e1"}), ("click", {"ref": "e1"}),
    ], disabled_actions=frozenset({"click"}), policy_block_limit=2)


def unknown_tool_name(tmp: Path) -> Prepared:
    return _plain(tmp, "unknown_tool_name", [
        ("teleport", {"where": "anywhere"}),
        ("finish", {"done": False, "why": "there is no such tool"}),
    ])


def step_budget_exhausted(tmp: Path) -> Prepared:
    return _plain(tmp, "step_budget_exhausted",
                  [("observe", {})] * 4, step_budget=2)


def wall_budget_exhausted(tmp: Path) -> Prepared:
    return _plain(tmp, "wall_budget_exhausted", [("observe", {})], wall_budget_s=0)


def control_plane_unreadable(tmp: Path) -> Prepared:
    chat = ScriptedChat([("observe", {})])
    browser = ScriptedBrowser()
    session = _session(tmp, "control_plane_unreadable", chat=chat, browser=browser,
                       control=ScriptedControl([[], []], unreadable_after=1))
    return Prepared(session=session, chat=chat, browser=browser)


def user_cancelled_before_the_first_call(tmp: Path) -> Prepared:
    return _plain(tmp, "user_cancelled_before_the_first_call",
                  [("observe", {})], cancelled=lambda: True)


def provider_defect_still_ends_the_run(tmp: Path) -> Prepared:
    chat = ExplodingChat([])
    browser = ScriptedBrowser()
    control = ScriptedControl([[], []])
    session = _session(tmp, "provider_defect_still_ends_the_run",
                       chat=chat, browser=browser, control=control)
    return Prepared(session=session, chat=chat, browser=browser,
                    extra=lambda: {"control_runs_begun": list(control.begun),
                                   "control_runs_ended": list(control.ended)})


def gateway_refused_the_write(tmp: Path) -> Prepared:
    chat = ScriptedChat([
        ("click", {"ref": "e1"}),
        ("finish", {"done": False, "why": "the write was refused"}),
    ])
    browser = ScriptedBrowser(links=(("e1", "Sprout", "/glasshouse/do/sproutSeedling"),))
    session = _session(tmp, "gateway_refused_the_write", chat=chat, browser=browser,
                       control=ScriptedControl([[], BLOCKED_WRITE, [], []]))
    return Prepared(session=session, chat=chat, browser=browser)


def gateway_permitted_the_write(tmp: Path) -> Prepared:
    chat = ScriptedChat([
        ("click", {"ref": "e1"}),
        ("observe", {}),
        ("finish", {"done": True, "why": "the page shows the new seedling"}),
    ])
    browser = ScriptedBrowser(links=(("e1", "Sprout", "/glasshouse/do/sproutSeedling"),))
    session = _session(tmp, "gateway_permitted_the_write", chat=chat, browser=browser,
                       control=ScriptedControl([[], PERMITTED_WRITE, [], []]))
    return Prepared(session=session, chat=chat, browser=browser)


def a_second_write_while_one_is_unconfirmed_is_indeterminate(tmp: Path) -> Prepared:
    chat = ScriptedChat([
        ("click", {"ref": "e1"}),
        ("click", {"ref": "e1"}),
        ("finish", {"done": True, "why": "both writes went through"}),
    ])
    browser = ScriptedBrowser(links=(("e1", "Sprout", "/glasshouse/do/sproutSeedling"),))
    session = _session(tmp, "a_second_write_while_one_is_unconfirmed_is_indeterminate",
                       chat=chat, browser=browser,
                       control=ScriptedControl([[], PERMITTED_WRITE, [], []]))
    return Prepared(session=session, chat=chat, browser=browser)


def business_read_projected_as_a_plain_answer(tmp: Path) -> Prepared:
    skill = read_skill_artifact(tmp / "read-skill.json")
    executor = QueryExecutor()
    chat = ScriptedChat([
        ("load_skill", {"skill_id": skill.skill_id}),
        ("query_trays", {}),
        (None, "The seed trays are listed above."),
    ])
    browser = ScriptedBrowser()
    session = _session(
        tmp, "business_read_projected_as_a_plain_answer", chat=chat, browser=browser,
        control=ReadOnlyControl(),
        business_tools=BusinessToolRuntime((skill,), executor),
        plain_business_answer_min_page_actions=0)
    return Prepared(session=session, chat=chat, browser=browser,
                    extra=lambda: {"executor_calls": executor.calls})


def business_read_then_open_the_authorised_route(tmp: Path) -> Prepared:
    skill = read_skill_artifact(tmp / "read-skill.json")
    executor = QueryExecutor(projected=False)
    chat = ScriptedChat([
        ("load_skill", {"skill_id": skill.skill_id}),
        ("query_trays", {"name_contains": "Tray A"}),
        ("goto", {"url": "/glasshouse/do/tray?id=1"}),
        ("finish", {"done": True, "why": "the tray record is open"}),
    ])
    browser = ScriptedBrowser()
    session = _session(
        tmp, "business_read_then_open_the_authorised_route", chat=chat, browser=browser,
        control=ReadOnlyControl(),
        business_tools=BusinessToolRuntime((skill,), executor))
    return Prepared(session=session, chat=chat, browser=browser)


def business_write_approved(tmp: Path) -> Prepared:
    skill = write_skill_artifact(tmp / "write-skill.json")
    executor = MutationExecutor()
    chat = ScriptedChat([
        ("load_skill", {"skill_id": skill.skill_id}),
        ("create_tray", {"name": "Tray B"}),
        ("finish", {"done": True, "why": "the tray was created"}),
    ])
    browser = ScriptedBrowser()
    session = _session(
        tmp, "business_write_approved", chat=chat, browser=browser,
        control=ReadOnlyControl(),
        task=TaskSpec(task_ref="T", prompt="Create tray Tray B.",
                      supported_scope="Create exactly one seed tray."),
        business_tools=BusinessToolRuntime((skill,), executor),
        business_mutation_approval=approve)
    return Prepared(session=session, chat=chat, browser=browser,
                    extra=lambda: {"executions": executor.executions})


def business_write_denied(tmp: Path) -> Prepared:
    skill = write_skill_artifact(tmp / "write-skill.json")
    executor = MutationExecutor()
    chat = ScriptedChat([
        ("load_skill", {"skill_id": skill.skill_id}),
        ("create_tray", {"name": "Tray B"}),
    ])
    browser = ScriptedBrowser()
    session = _session(
        tmp, "business_write_denied", chat=chat, browser=browser,
        control=ReadOnlyControl(),
        task=TaskSpec(task_ref="T", prompt="Create tray Tray B.",
                      supported_scope="Create exactly one seed tray."),
        business_tools=BusinessToolRuntime((skill,), executor),
        business_mutation_approval=deny)
    return Prepared(session=session, chat=chat, browser=browser,
                    extra=lambda: {"executions": executor.executions})


def business_write_indeterminate(tmp: Path) -> Prepared:
    skill = write_skill_artifact(tmp / "write-skill.json")
    executor = MutationExecutor(indeterminate=True)
    chat = ScriptedChat([
        ("load_skill", {"skill_id": skill.skill_id}),
        ("create_tray", {"name": "Tray B"}),
        ("finish", {"done": True, "why": "it probably worked"}),
    ])
    browser = ScriptedBrowser()
    session = _session(
        tmp, "business_write_indeterminate", chat=chat, browser=browser,
        control=ReadOnlyControl(),
        task=TaskSpec(task_ref="T", prompt="Create tray Tray B.",
                      supported_scope="Create exactly one seed tray."),
        business_tools=BusinessToolRuntime((skill,), executor),
        business_mutation_approval=approve)
    return Prepared(session=session, chat=chat, browser=browser,
                    extra=lambda: {"executions": executor.executions})


def skill_route_is_resolved_server_side(tmp: Path) -> Prepared:
    chat = ScriptedChat([
        ("open_skill_route", {"skill_id": TRAY_SKILL.skill_id, "bindings": {}}),
        ("finish", {"done": True, "why": "the tray list is open"}),
    ])
    browser = ScriptedBrowser()
    session = _session(
        tmp, "skill_route_is_resolved_server_side", chat=chat, browser=browser,
        knowledge_router=FixtureKnowledgeRouter((TRAY_SKILL,)))
    return Prepared(session=session, chat=chat, browser=browser)


def application_entry_uses_a_page_local_link(tmp: Path) -> Prepared:
    chat = ScriptedChat([
        ("enter_application", {"ref": "e1"}),
        ("finish", {"done": True, "why": "the application is open"}),
    ])
    browser = ScriptedBrowser(
        start=observation(1, "http://app.test/portal",
                          links=(("e1", "Glasshouse", "/glasshouse/do/main"),)),
        links=(("e1", "Glasshouse", "/glasshouse/do/main"),))
    session = _session(tmp, "application_entry_uses_a_page_local_link",
                       chat=chat, browser=browser, allow_application_entry=True)
    return Prepared(session=session, chat=chat, browser=browser)


def source_candidates_are_delivered_with_citations(tmp: Path) -> Prepared:
    source_map = fixture_source_map(tmp / "source")
    chat = ScriptedChat([
        ("open_source_candidate", {"candidate_id": "c1"}),
        ("finish", {"done": True, "why": "the cited route is open"}),
    ])
    browser = ScriptedBrowser()
    session = _session(
        tmp, "source_candidates_are_delivered_with_citations", chat=chat,
        browser=browser, source=SourceCapability(
            source_map=source_map, source_affordance_mode="auto"))
    session.adopt_entry(browser.observe())
    return Prepared(session=session, chat=chat, browser=browser)


def runtime_source_lookup_returns_cited_evidence(tmp: Path) -> Prepared:
    source_map = fixture_source_map(tmp / "source")
    chat = ScriptedChat([
        ("lookup_source", {"query": "trays"}),
        ("finish", {"done": True, "why": "the source answered the question"}),
    ])
    browser = ScriptedBrowser()
    session = _session(
        tmp, "runtime_source_lookup_returns_cited_evidence", chat=chat, browser=browser,
        source=SourceCapability(source_map=source_map, runtime_source=True))
    session.adopt_entry(browser.observe())
    return Prepared(session=session, chat=chat, browser=browser)


def goto_outside_the_application_grammar_is_refused(tmp: Path) -> Prepared:
    return _plain(tmp, "goto_outside_the_application_grammar_is_refused", [
        ("goto", {"url": "https://elsewhere.invalid/steal"}),
        ("finish", {"done": False, "why": "that route is not available"}),
    ])


def a_referential_followup_keeps_its_prior_domain(tmp: Path) -> Prepared:
    skill = read_skill_artifact(tmp / "read-skill.json")
    executor = QueryExecutor()
    chat = ScriptedChat([
        ("load_skill", {"skill_id": skill.skill_id}),
        ("query_trays", {}),
        (None, "That is the one you asked about."),
    ])
    browser = ScriptedBrowser()
    session = _session(
        tmp, "a_referential_followup_keeps_its_prior_domain", chat=chat, browser=browser,
        control=ReadOnlyControl(),
        task=TaskSpec(task_ref="T", prompt="Open the second one.",
                      supported_scope="Read the current application only."),
        business_tools=BusinessToolRuntime((skill,), executor),
        plain_business_answer_min_page_actions=0,
        conversation_context={
            "schema_version": 1,
            "prior_turns": [{"message_id": "m1", "user": "Which trays are there?",
                             "assistant": "Tray A.", "status": "finished"}],
        })
    return Prepared(session=session, chat=chat, browser=browser)


SCENARIOS: tuple[Scenario, ...] = (
    Scenario("refusal", "the agent declines without touching the page", refusal),
    Scenario("read_and_finish", "the shortest successful read path", read_and_finish),
    Scenario("page_action_then_finish", "navigation and a wait, then a claim",
             page_action_then_finish),
    Scenario("disabled_action_is_refused_before_dispatch",
             "a server action policy refuses before the browser is asked",
             disabled_action_is_refused_before_dispatch),
    Scenario("repeated_policy_blocks_end_the_run",
             "the block limit is a terminal state, not an infinite loop",
             repeated_policy_blocks_end_the_run),
    Scenario("unknown_tool_name", "an invented tool name is an ordinary ReAct result",
             unknown_tool_name),
    Scenario("step_budget_exhausted", "the step budget terminates the loop",
             step_budget_exhausted),
    Scenario("wall_budget_exhausted", "the wall budget terminates before the first call",
             wall_budget_exhausted),
    Scenario("control_plane_unreadable", "an unreadable control plane is terminal",
             control_plane_unreadable),
    Scenario("user_cancelled_before_the_first_call", "cancellation precedes every dispatch",
             user_cancelled_before_the_first_call),
    Scenario("provider_defect_still_ends_the_run",
             "an unexpected provider exception still releases the control run",
             provider_defect_still_ends_the_run),
    Scenario("gateway_refused_the_write", "a refused write is reported to the agent",
             gateway_refused_the_write),
    Scenario("gateway_permitted_the_write",
             "a permitted write is not an acknowledgement", gateway_permitted_the_write),
    Scenario("a_second_write_while_one_is_unconfirmed_is_indeterminate",
             "the run stops rather than stacking unconfirmed writes",
             a_second_write_while_one_is_unconfirmed_is_indeterminate),
    Scenario("business_read_projected_as_a_plain_answer",
             "a typed read ends the loop with a projectable answer",
             business_read_projected_as_a_plain_answer),
    Scenario("business_read_then_open_the_authorised_route",
             "only a route the ToolResult authorised may be opened",
             business_read_then_open_the_authorised_route),
    Scenario("business_write_approved", "proposal, approval, one execution, read-back",
             business_write_approved),
    Scenario("business_write_denied", "denial is terminal and executes nothing",
             business_write_denied),
    Scenario("business_write_indeterminate",
             "a lost acknowledgement is preserved, never retried",
             business_write_indeterminate),
    Scenario("skill_route_is_resolved_server_side",
             "the route template never reaches the model",
             skill_route_is_resolved_server_side),
    Scenario("application_entry_uses_a_page_local_link",
             "entry is a page-local ref, not a URL the model composed",
             application_entry_uses_a_page_local_link),
    Scenario("source_candidates_are_delivered_with_citations",
             "compiled source reaches the turn as cited candidates",
             source_candidates_are_delivered_with_citations),
    Scenario("runtime_source_lookup_returns_cited_evidence",
             "explicit source retrieval is bounded and cited",
             runtime_source_lookup_returns_cited_evidence),
    Scenario("goto_outside_the_application_grammar_is_refused",
             "the route grammar bounds where a run may go",
             goto_outside_the_application_grammar_is_refused),
    Scenario("a_referential_followup_keeps_its_prior_domain",
             "prior user intent, and only that, may recover a domain",
             a_referential_followup_keeps_its_prior_domain),
)


# --------------------------------------------------------------- tool surfaces


def tool_surface_compositions(tmp: Path) -> dict[str, ServerAgentSession]:
    """Every composition whose tool surface is a public contract with the model.

    A surface change is a product change: it alters what the model can attempt, which is
    the one thing a deployment cannot observe from the outside. Recorded in full rather
    than as a digest so a re-record shows *which* tool moved.
    """
    read_skill = read_skill_artifact(tmp / "surface-read.json")
    write_skill = write_skill_artifact(tmp / "surface-write.json")
    source_map = fixture_source_map(tmp / "surface-source")
    surfaces: dict[str, ServerAgentSession] = {}

    def add(name: str, **overrides):
        surfaces[name] = _session(tmp, f"surface-{name}", chat=ScriptedChat([]),
                                  browser=ScriptedBrowser(), **overrides)

    add("browser_only")
    add("browser_without_click", disabled_actions=frozenset({"click"}))
    add("with_knowledge_router", knowledge_router=FixtureKnowledgeRouter((TRAY_SKILL,)))
    add("with_application_entry", allow_application_entry=True)
    add("with_runtime_source",
        source=SourceCapability(source_map=source_map, runtime_source=True))
    add("with_source_affordances", source=SourceCapability(
        source_map=source_map, source_affordance_mode="auto"))
    add("with_read_only_business_skill", control=ReadOnlyControl(),
        business_tools=BusinessToolRuntime((read_skill,), QueryExecutor()))
    add("with_mutation_business_skill", control=ReadOnlyControl(),
        task=TaskSpec(task_ref="T", prompt="Create tray Tray B.",
                      supported_scope="Create exactly one seed tray."),
        business_tools=BusinessToolRuntime((write_skill,), MutationExecutor()),
        business_mutation_approval=approve)
    return surfaces
