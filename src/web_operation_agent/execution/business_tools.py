"""Typed business tool dispatch and stable session-facing helpers."""
from __future__ import annotations

import json

from .business_mutations import execute_business_mutation, observe_after_business_mutation
from .business_reads import execute_business_read
from .form_tools import apply_form_draft, prefill_form
from .runstate import _Next, _Turn


def business_tool(session, turn: "_Turn", call: dict, name: str, args: dict) -> "_Next":
    """Dispatch a typed business tool after binding its exact operation context."""
    operation_context = session._approvals.context(turn.step, name)
    operation_identity = (operation_context.audit_projection()
                          if operation_context is not None else {})
    if session.business_tools.is_mutation(name):
        return execute_business_mutation(
            session, turn, call, name, args, operation_context, operation_identity)
    return execute_business_read(
        session, turn, call, name, args, operation_context, operation_identity)


def load_skill(session, turn: "_Turn", call: dict, name: str, args: dict) -> "_Next":
    """Load one of this task's candidate Business Skills: a playbook, not runtime data."""
    skill, rejection = session.business_tools.load(
        args.get("skill_id"), session._task_business_skill_ids)
    if rejection:
        session.ledger.append(
            "business_skill_rejected", step=turn.step,
            skill_id=str(args.get("skill_id") or "")[:200], reason=rejection,
            side_effect_possible=False)
        return session._reply(turn, call, "The server rejected load_skill: " + rejection + ".")
    assert skill is not None
    session._loaded_business_skill_ids.add(skill.skill_id)
    session.ledger.append(
        "business_skill_loaded", step=turn.step, skill_id=skill.skill_id,
        skill_sha256=skill.artifact_sha256,
        tool_names=list(skill.tool_names), provenance=list(skill.provenance))
    return session._reply(turn, call,
                       "BUSINESS SKILL LOADED (playbook; not runtime data):\n"
                       + json.dumps(skill.loaded_card(), ensure_ascii=False,
                                    sort_keys=True, separators=(",", ":")))


def render_business_skills(session) -> str:
    if not session._task_business_skills:
        return ""
    if session._skills_unmatched:
        # An index, not a shortlist: id, title and whether it reads only. The intent and
        # tool list are what made an unrelated Skill look like a plan for the request.
        cards = [skill.loaded_card() if skill.skill_id in session._loaded_business_skill_ids
                 else {"skill_id": skill.skill_id, "title": skill.title,
                       "read_only": skill.read_only}
                 for skill in session._task_business_skills]
        return ("BUSINESS SKILLS NOT MATCHED TO THIS REQUEST (no Skill trigger matched its "
                "wording; load one only if the request needs current business records):\n"
                + json.dumps(cards, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")))
    if not session._auto_loaded_business_skill_ids:
        cards = [skill.candidate_card(loaded=False)
                 if skill.skill_id not in session._loaded_business_skill_ids
                 else skill.candidate_card(loaded=True)
                 for skill in session._task_business_skills]
        return ("BUSINESS SKILL CANDIDATES (load before using their tools):\n"
                + json.dumps(cards, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")))
    cards = [
        skill.loaded_card()
        if skill.skill_id in session._auto_loaded_business_skill_ids
        else skill.candidate_card(loaded=False)
        for skill in session._task_business_skills]
    return ("BUSINESS SKILLS (loaded cards include their playbook; load remaining "
            "candidates before using their tools):\n"
            + json.dumps(cards, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")))
