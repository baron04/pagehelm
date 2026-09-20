"""The model-visible tool surface for one server-side agent session.

Availability is policy, not prompt text: this object turns the session's current factual state
into the exact schemas the provider may call.  Step handlers move here next; keeping the
surface separate first prevents their availability rules from being duplicated as they leave
``ServerAgentSession``.
"""
from __future__ import annotations

from ..answer import answer_protocol, page_answer
from .knowledge_tools import capability_check_available
from .prompts import (APPLICATION_ENTRY_TOOL, ASK_USER_TOOL, CAPABILITY_TOOL,
                      LOAD_SKILL_TOOL, SOURCE_CANDIDATE_TOOL, SOURCE_NAV_TOOL,
                      SOURCE_TOOL, SKILL_APP_TOOL, SKILL_ROUTE_TOOL, TOOL_SCHEMAS,
                      WITHDRAW_TOOL, PREFILL_FORM_TOOL)


class ToolSurface:
    """Build the currently valid tool schema list from one session's server state."""

    def __init__(self, session):
        self._session = session

    def schemas(self) -> list[dict]:
        session = self._session
        tools = [tool for tool in TOOL_SCHEMAS
                 if tool["function"]["name"] not in session.disabled_actions]
        if session._capability_question:
            return [tool for tool in tools if tool["function"]["name"] == "finish"]
        if session.page_answer_evidence:
            tools = [page_answer.OBSERVE_TOOL if tool["function"]["name"] == "observe"
                     else tool for tool in tools]
        if session._answers.answer_mode:
            tools = [answer_protocol.FINISH_ANSWER_TOOL if tool["function"]["name"] == "finish"
                     else tool for tool in tools]
        if session.allow_clarification:
            tools += [ASK_USER_TOOL]
        if capability_check_available(session):
            tools += [CAPABILITY_TOOL]
        if session._answers_pending_question:
            tools += [WITHDRAW_TOOL]
        if len(session.evidence):
            tools += [answer_protocol.COMPUTE_TOOL]
        tools += [SOURCE_TOOL, SOURCE_NAV_TOOL] if session.runtime_source else []
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
        if session.allow_form_prefill and session._form_draft is not None:
            tools += [PREFILL_FORM_TOOL]
        return tools
