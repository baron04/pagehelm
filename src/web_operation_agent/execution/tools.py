"""The model-visible tool surface for one server-side agent session.

Availability is policy, not prompt text: this object turns the session's current factual state
into the exact schemas the provider may call.  Step handlers move here next; keeping the
surface separate first prevents their availability rules from being duplicated as they leave
``ServerAgentSession``.
"""
from __future__ import annotations

from copy import deepcopy

from ..answer import answer_protocol, page_answer
from ..business.forms import FORM_TOOL_SCHEMAS
from ..business.form_entries import FORM_ENTRY_SCHEMAS
from ..business.goal_candidates import RESOLVE_GOAL_CANDIDATES_TOOL
from ..goal.read_schemas import DATA_READ_SCHEMAS
from ..goal.read_tools import DATA_READ_NAMES
from .knowledge_tools import capability_check_available
from .request_contract import DECLARE_TASK_TOOL, finish_schema
from .prompts import (APPLICATION_ENTRY_TOOL, ASK_USER_TOOL, CAPABILITY_TOOL,
                      LOAD_SKILL_TOOL, SOURCE_CANDIDATE_TOOL, SOURCE_NAV_TOOL,
                      SOURCE_TOOL, SOURCE_HISTORY_TOOL, SOURCE_READ_TOOL, SKILL_APP_TOOL, SKILL_ROUTE_TOOL,
                      TARGET_TOOL, FACET_TOOL, TOOL_SCHEMAS,
                      WITHDRAW_TOOL, PREFILL_FORM_TOOL)


class ToolSurface:
    """Build the currently valid tool schema list from one session's server state."""

    def __init__(self, session):
        self._session = session

    def schemas(self) -> list[dict]:
        session = self._session
        if session.require_task_contract and session._task_requirements is None:
            return [deepcopy(DECLARE_TASK_TOOL)]
        tools = [tool for tool in TOOL_SCHEMAS
                 if tool["function"]["name"] not in session.disabled_actions
                 or (tool["function"]["name"] == "click"
                     and session.click_policy is not None)
                 or (session.action_admission is not None
                     and tool["function"]["name"] in session.action_admission.actions)]
        skill = bool(session._loaded_business_skill_ids)
        if session.require_task_contract:
            tools += [deepcopy(DECLARE_TASK_TOOL)]
        if session._finalizing:
            # Answer repair may need one missing typed read or a fresh snapshot.
            # Build the ordinary actor-scoped schemas below, then retain only reads.
            tools = [page_answer.OBSERVE_TOOL,
                     answer_protocol.finish_answer_tool(source=session.runtime_source,
                                                       skill=skill)]
        if session._capability_question:
            tools = [tool for tool in tools if tool["function"]["name"] == "finish"]
            if session._task_requirements and session._task_requirements.answer_requests:
                tools = [finish_schema(answer_protocol.finish_answer_tool(), session._task_requirements)]
            return tools
        if session.page_answer_evidence:
            tools = [page_answer.OBSERVE_TOOL if tool["function"]["name"] == "observe"
                     else tool for tool in tools]
        if session._answers.answer_mode:
            finish = answer_protocol.finish_answer_tool(source=session.runtime_source,
                                                        skill=skill)
            tools = [finish if tool["function"]["name"] == "finish" else tool
                     for tool in tools]
        elif session.evidence.has_source_evidence():
            # Source is evidence, not a task-mode switch. One structured finish
            # supports either a cited answer or an observed action receipt.
            finish = answer_protocol.finish_answer_tool(source=True, skill=skill)
            tools = [finish if tool["function"]["name"] == "finish" else tool
                     for tool in tools]
        if session.target_settlement_available():
            tools += [TARGET_TOOL]
        if session.allow_page_facets and "apply_facet" not in session.disabled_actions:
            tools += [FACET_TOOL]
        if session.allow_clarification:
            tools += [ASK_USER_TOOL]
        if capability_check_available(session):
            tools += [CAPABILITY_TOOL]
        if session._answers_pending_question:
            tools += [WITHDRAW_TOOL]
        # Offered for the whole turn wherever it can ever apply, rather than from the first
        # typed result on: a tool list that changes invalidates every cached message after
        # it. Without evidence the server refuses the call, as it always has.
        if (session.business_tools is not None
                or session.data_reads is not None
                or session.evidence.has_application_evidence()):
            tools += [answer_protocol.COMPUTE_TOOL]
        if session.data_reads is not None:
            tools += list(DATA_READ_SCHEMAS)
        if session.runtime_source:
            tools += [SOURCE_TOOL, SOURCE_HISTORY_TOOL]
            if session.source.reader_lookup() is not None:
                tools += [SOURCE_READ_TOOL]
            if session.source_map is not None:
                tools += [SOURCE_NAV_TOOL]
        if session.source_affordance_mode in ("auto", "empty"):
            tools += [SOURCE_CANDIDATE_TOOL]
        if session.knowledge_router is not None:
            tools += [SKILL_APP_TOOL, SKILL_ROUTE_TOOL]
        if session.allow_application_entry:
            tools += [APPLICATION_ENTRY_TOOL]
        if session._task_business_skills:
            if (not session._auto_loaded_business_skill_ids
                    or session._loaded_business_skill_ids != session._task_business_skill_ids):
                tools += [LOAD_SKILL_TOOL]
        if session.business_tools is not None:
            principal = session._approvals.principal
            business_schemas = session.business_tools.available_schemas(
                allow_mutations=session._approvals.mutations_enabled,
                actor_scopes=(principal.scopes if principal is not None else None),
                writes=session._requested_writes)
            read_names = session.business_tools.read_tool_names & {
                schema["function"]["name"] for schema in business_schemas}
            metric_read_names = read_names - {RESOLVE_GOAL_CANDIDATES_TOOL}
            goal_property = {
                "type": "array", "minItems": 0, "maxItems": 4,
                "description": (
                    "Required on the FIRST business read. For a current count or sum "
                    "question, name every requested read collection and metric, even if "
                    "this call only resolves an identity. For other tasks pass []. These "
                    "goals freeze before results and cannot be changed later."),
                "items": {"type": "object", "additionalProperties": False,
                          "properties": {
                              "target_read_tool": {"type": "string",
                                                   "enum": sorted(metric_read_names)},
                              "measures": {"type": "array", "minItems": 1,
                                           "maxItems": 2, "uniqueItems": True,
                                           "items": {"type": "string",
                                                     "enum": ["count", "sum"]}},
                          },
                          "required": ["target_read_tool", "measures"]},
            }
            for schema in business_schemas:
                if (session.business_tools.numeric_goal_binding
                      and not session._answers.first_business_read_seen
                      and schema["function"]["name"] != RESOLVE_GOAL_CANDIDATES_TOOL
                      and schema["function"]["name"] in read_names):
                    schema = deepcopy(schema)
                    parameters = schema["function"]["parameters"]
                    parameters["properties"]["answer_goals"] = goal_property
                    parameters["required"] = list(dict.fromkeys(
                        [*(parameters.get("required") or []), "answer_goals"]))
                tools.append(schema)
        if (session.allow_form_prefill
                and session._application_form_tools is not None):
            tools += list(FORM_TOOL_SCHEMAS)
            if session._form_entry_tools is not None:
                tools += list(FORM_ENTRY_SCHEMAS)
        if (session.allow_form_prefill
                and (session._application_form_tools is not None
                     or session.business_tools is not None)):
            # Same reason as compute: offered while a draft can exist, refused without one.
            tools += [PREFILL_FORM_TOOL]
        # A reached target does not shrink this list: the loop refuses every tool but
        # finish and set_target without dispatch, and the prompt says so. Shrinking it
        # changed the request prefix at the one step that most needs to be cheap.
        if session._finalizing:
            permitted = answer_repair_tools(session)
            tools = [tool for tool in tools if tool["function"]["name"] in permitted]
        if session._task_requirements and session._task_requirements.answer_requests:
            tools = [finish_schema(tool, session._task_requirements)
                     if tool["function"]["name"] == "finish" else tool for tool in tools]
        return tools


def answer_repair_tools(session) -> frozenset[str]:
    """Only observation, evidence arithmetic and registered reads after an action.

    The same set controls schema exposure and dispatch. Write/form/page-action tools
    cannot run again just because the action's accompanying answer was invalid.
    """
    reads = (session.business_tools.read_tool_names
             if session.business_tools is not None else frozenset())
    source_reads = (frozenset({"lookup_source", "lookup_source_history"})
                    if session.runtime_source else frozenset())
    if session.runtime_source and session.source.reader_lookup() is not None:
        source_reads |= {"read_source"}
    data_reads = DATA_READ_NAMES if session.data_reads is not None else frozenset()
    return frozenset({"finish", "compute", "observe"}) | reads | source_reads | data_reads
