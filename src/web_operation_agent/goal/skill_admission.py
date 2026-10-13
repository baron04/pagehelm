"""One reversible consultation before an ordinary goal exits or hands over."""
from __future__ import annotations

from ..business.effects import canonical
from ..modeling.client import ProviderError
from .skill_selection import selection_tool, selected_ids
from .understand import _declaration_from


class SkillProposalYield(Exception):
    """No installed execution proposal owns this request; no approval has begun."""

    def __init__(self, limit=""):
        self.limit = limit


def yield_tool():
    return {"type": "function", "function": {
        "name": "continue_goal",
        "description": "No installed execution applies. Continue the ordinary query, page, explanation or preparation path; do not execute.",
        "parameters": {"type": "object", "properties": {},
                       "additionalProperties": False}}}


def check_yield(reply):
    calls = reply.get("tool_calls") if isinstance(reply, dict) else None
    if not isinstance(calls, list):
        return
    if any(isinstance(call, dict) and isinstance(call.get("function"), dict)
           and call["function"].get("name") == "continue_goal" for call in calls):
        if len(calls) != 1:
            raise ValueError("continue_goal must be the only declaration")
        args, _ = _declaration_from(reply, "continue_goal")
        if args != {}:
            raise ValueError("continue_goal takes no arguments")
        raise SkillProposalYield()


def _select(state, candidates):
    reply = state.chat.complete_cancellable([
        {"role": "system", "content":
            "Select installed Skills only if the complete user request asks to EXECUTE a covered "
            "business change. Queries, explanations, negation and preparation use continue_goal. "
            "Uncovered changes also use continue_goal. Return identities only; no values or steps. "
            "The earlier scope may be incomplete. Selection is knowledge, never write authority."},
        {"role": "user", "content": "INSTALLED SKILLS\n" + canonical(list(candidates)) +
            "\nREQUEST\n" + state.message.text}],
        [selection_tool(candidates), yield_tool()], state.run_id,
        cancelled=state.cancelled, timeout_s=max(1.0, state.budget.remaining()),
        max_tokens=200, options={"tool_choice": "required"})
    if not isinstance(reply, dict) or not isinstance(reply.get("tool_calls"), list) or len(reply["tool_calls"]) != 1:
        raise ValueError("Skill consultation requires one tool declaration")
    check_yield(reply)
    call = reply["tool_calls"][0]
    if not isinstance(call, dict) or not isinstance(call.get("function"), dict) or call["function"].get("name") != "select_skills":
        raise ValueError("Skill consultation calls an unavailable tool")
    args, _ = _declaration_from(reply, "select_skills")
    return selected_ids(args, candidates)


def consult_execution(executor, state, *, entities=()):
    """Return an owned terminal reply, or None to resume the untouched goal path."""
    s = state
    operations = executor.application.ports.operations
    if (not executor.config.allow_business_mutations or operations is None
            or s.skill_consulted or s.mutation_outcomes):
        return None
    eligible = tuple(skill for skill in operations.skills.artifacts
        if any(d.effect_contract is not None and d.name in skill.write_operations
               for d in operations.tools.definitions))
    if not eligible:
        return None
    from .skill_execution import skills_for_scope, execute_skill_plan
    matched = set(skills_for_scope(operations,
        [*s.scope_parts, *({"entity": entity} for entity in entities)], limit=None))
    candidates = tuple(skill for skill in eligible if skill.skill_id in matched) or eligible
    s.skill_consulted = True
    s.ledger.append("goal_skill_consulted", step=s.step, matched=len(matched),
                    side_effect_possible=False)
    # Only these planning projections may change before ownership. Budgets and
    # audit remain consumed; no ordinary decision sees consultation candidates.
    original = (s.references, s.candidates, s.index, s.write_intent, s.unsupported)
    try:
        if matched and 1 <= len(candidates) <= 3:
            selected = tuple(skill.skill_id for skill in candidates)
        else:
            with s.receipt.stage("scope"):
                selected = _select(s, tuple(skill.candidate_card() for skill in candidates))
        answer = execute_skill_plan(executor, s, selected,
                                    native_proposals=True, allow_yield=True)
        if answer is not None:
            return answer
    except SkillProposalYield as exc:
        s.skill_limit = exc.limit
    except ValueError:
        s.ledger.append("skill_consultation_invalid", side_effect_possible=False)
    except ProviderError as exc:
        from .executor import _provider_failed
        s.terminal_reason, s.detail = "provider_error", str(exc)[:200]
        s.ledger.append("provider_failed", step=s.step,
                        provider_error_code=getattr(exc, "code", "provider_error"))
        return executor._say(s, _provider_failed(exc, s.chinese))
    s.references, s.candidates, s.index, s.write_intent, s.unsupported = original
    s.ledger.append("goal_skill_yielded", step=s.step, side_effect_possible=False)
    return None
