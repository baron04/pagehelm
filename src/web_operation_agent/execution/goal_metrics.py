"""Derive requested, fully scoped numeric evidence without another model round."""
from __future__ import annotations

import time

from ..answer.computation import ComputationRefused, ComputationRequest, run
from ..answer.evidence import QueryEvidence, TurnEvidence
from ..answer.goal_scope import AnswerGoal


def materialize_goal_metrics(turn: TurnEvidence, recorded: tuple[str, ...], *,
                             tool: str, goals: tuple[AnswerGoal, ...],
                             step: int) -> tuple[str, ...]:
    """Compute only what the frozen goal requested from this read's verified query.

    An exact count needs no returned rows. A sum is attempted only when the adapter
    certified exactly one additive field; the existing computation contract still
    refuses partial rows, mixed currencies and unread values. A refusal here is not a
    guessed zero or a new model step: the Agent can make a different bounded read.
    """
    goal = next((item for item in goals if item.target_read_tool == tool), None)
    queries = [ref for ref in recorded
               if isinstance(turn.get(ref), QueryEvidence)]
    if goal is None or len(queries) != 1:
        return ()
    query_ref = queries[0]
    query = turn.get(query_ref)
    assert isinstance(query, QueryEvidence)
    produced = []
    for measure in ("count", "sum"):
        if measure not in goal.measures:
            continue
        if measure == "count" and not query.count_exact:
            continue
        if (measure == "sum"
                and (not query.covers_whole_scope
                     or len(query.summable_fields) != 1)):
            continue
        request = ComputationRequest(
            operation=measure, over=(query_ref,),
            field_name=query.summable_fields[0] if measure == "sum" else "")
        try:
            item = run(turn, request, evidence_id=f"goal-{measure}#{step}",
                       observed_at=str(time.time()))
        except ComputationRefused:
            continue
        if item.covers_whole_scope:
            produced.append(item.evidence_id)
    return tuple(produced)


__all__ = ["materialize_goal_metrics"]
