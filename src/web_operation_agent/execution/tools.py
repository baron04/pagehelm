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
from .knowledge_tools import capability_check_available
from .prompts import (APPLICATION_ENTRY_TOOL, ASK_USER_TOOL, CAPABILITY_TOOL,
                      LOAD_SKILL_TOOL, SOURCE_CANDIDATE_TOOL, SOURCE_NAV_TOOL,
                      SOURCE_TOOL, SOURCE_HISTORY_TOOL, SKILL_APP_TOOL, SKILL_ROUTE_TOOL,
                      TARGET_TOOL, FACET_TOOL, TOOL_SCHEMAS,
                      WITHDRAW_TOOL, PREFILL_FORM_TOOL)


class ToolSurface:
    """Build the currently valid tool schema list from one session's server state."""

    def __init__(self, session):
        self._session = session

    def schemas(self) -> list[dict]:
        session = self._session
        tools = [tool for tool in TOOL_SCHEMAS
                 if tool["function"]["name"] not in session.disabled_actions
                 or (tool["function"]["name"] == "click"
                     and session.click_policy is not None)]
        skill = bool(session._loaded_business_skill_ids)
        if session._finalizing:
            return [answer_protocol.finish_answer_tool(source=session.runtime_source,
                                                       skill=skill)]
        if session._capability_question:
            return [tool for tool in tools if tool["function"]["name"] == "finish"]
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
                or session.evidence.has_application_evidence()):
            tools += [answer_protocol.COMPUTE_TOOL]
        tools += [SOURCE_TOOL, SOURCE_HISTORY_TOOL, SOURCE_NAV_TOOL] \
            if session.runtime_source else []
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
                actor_scopes=(principal.scopes if principal is not None else None))
            read_names = session.business_tools.read_tool_names & {
                schema["function"]["name"] for schema in business_schemas}
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
                                                   "enum": sorted(read_names)},
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
        if (session.allow_form_prefill
                and (session._application_form_tools is not None
                     or session.business_tools is not None)):
            # Same reason as compute: offered while a draft can exist, refused without one.
            tools += [PREFILL_FORM_TOOL]
        # A reached target does not shrink this list: the loop refuses every tool but
        # finish and set_target without dispatch, and the prompt says so. Shrinking it
        # changed the request prefix at the one step that most needs to be cheap.
        return tools
