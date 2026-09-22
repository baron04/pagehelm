"""The model-visible tool surface for one server-side agent session.

Availability is policy, not prompt text: this object turns the session's current factual state
into the exact schemas the provider may call.  Step handlers move here next; keeping the
surface separate first prevents their availability rules from being duplicated as they leave
``ServerAgentSession``.
"""
from __future__ import annotations

from ..answer import answer_protocol, page_answer
from ..business.forms import FORM_TOOL_SCHEMAS
from .knowledge_tools import capability_check_available
from .prompts import (APPLICATION_ENTRY_TOOL, ASK_USER_TOOL, CAPABILITY_TOOL,
                      LOAD_SKILL_TOOL, SOURCE_CANDIDATE_TOOL, SOURCE_NAV_TOOL,
                      SOURCE_TOOL, SOURCE_HISTORY_TOOL, SKILL_APP_TOOL, SKILL_ROUTE_TOOL,
                      TARGET_TOOL, TOOL_SCHEMAS,
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
        if session._finalizing:
            return [answer_protocol.finish_answer_tool(source=session.runtime_source)]
        if session._capability_question:
            return [tool for tool in tools if tool["function"]["name"] == "finish"]
        if session.page_answer_evidence:
            tools = [page_answer.OBSERVE_TOOL if tool["function"]["name"] == "observe"
                     else tool for tool in tools]
        if session._answers.answer_mode:
            finish = answer_protocol.finish_answer_tool(source=session.runtime_source)
            tools = [finish if tool["function"]["name"] == "finish" else tool
                     for tool in tools]
        elif session.evidence.has_source_evidence():
            # Source is evidence, not a task-mode switch. One structured finish
            # supports either a cited answer or an observed action receipt.
            finish = answer_protocol.finish_answer_tool(source=True)
            tools = [finish if tool["function"]["name"] == "finish" else tool
                     for tool in tools]
        if session.target_settlement_available():
            tools += [TARGET_TOOL]
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
            tools += session.business_tools.schemas_for(session._loaded_business_skill_ids)
        if session.allow_form_prefill and session._application_form_tools is not None:
            tools += list(FORM_TOOL_SCHEMAS)
        if session.allow_form_prefill and (session._application_form_tools is not None
                                           or session.business_tools is not None):
            # Same reason as compute: offered while a draft can exist, refused without one.
            tools += [PREFILL_FORM_TOOL]
        # A reached target does not shrink this list: the loop refuses every tool but
        # finish and set_target without dispatch, and the prompt says so. Shrinking it
        # changed the request prefix at the one step that most needs to be cheap.
        return tools
