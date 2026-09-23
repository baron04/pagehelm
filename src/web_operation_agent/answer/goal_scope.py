"""Bind a read-backed answer to the collection named before its first read.

The model may investigate other collections, but a later nonempty result cannot silently
replace the collection it said it would answer.  Tool names are opaque handles here: an
application adapter owns their business meanings.  This is a deliberately small contract
for count and sum questions, not a natural-language intent verifier.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .evidence import (ComputationEvidence, QueryEvidence, SelectionEvidence,
                       TurnEvidence)


MEASURES = frozenset({"count", "sum"})
_TOOL_NAME = re.compile(r"[a-z][a-z0-9_]{0,99}\Z")


@dataclass(frozen=True, slots=True)
class AnswerGoal:
    """One requested metric in one application-defined read collection."""

    target_read_tool: str
    measures: frozenset[str]


def parse_answer_goals(value: object, *, read_tools: frozenset[str]
                       ) -> tuple[AnswerGoal, ...]:
    if not isinstance(value, list) or len(value) > 4:
        raise ValueError("answer_goals must contain zero to four requested collections")
    goals = []
    for entry in value:
        if (not isinstance(entry, dict)
                or set(entry) != {"target_read_tool", "measures"}):
            raise ValueError("each answer goal needs target_read_tool and measures")
        target = entry["target_read_tool"]
        measures = entry["measures"]
        if (not isinstance(target, str) or not _TOOL_NAME.fullmatch(target)
                or target not in read_tools):
            raise ValueError("answer goal must name an offered read tool")
        if (not isinstance(measures, list) or not 1 <= len(measures) <= 2
                or any(not isinstance(item, str) or item not in MEASURES
                       for item in measures)
                or len(set(measures)) != len(measures)):
            raise ValueError("answer goal measures must be count and/or sum")
        goals.append(AnswerGoal(target, frozenset(measures)))
    if len({goal.target_read_tool for goal in goals}) != len(goals):
        raise ValueError("a read collection may occur only once in answer_goals")
    return tuple(goals)


def _query_root(turn: TurnEvidence, ref: str) -> QueryEvidence | None:
    item = turn.get(ref)
    if isinstance(item, (QueryEvidence, SelectionEvidence)):
        return turn.query_for(ref)
    if isinstance(item, ComputationEvidence):
        roots = {root.evidence_id: root for input_ref in item.inputs
                 if (root := _query_root(turn, input_ref)) is not None}
        if len(roots) == 1:
            return next(iter(roots.values()))
    return None


def _measure(turn: TurnEvidence, ref: str) -> str | None:
    item = turn.get(ref)
    if not isinstance(item, ComputationEvidence) or not item.covers_whole_scope:
        return None
    if item.operation == "count()" and item.value_type == "count":
        return "count"
    if item.operation.startswith("sum(") or item.operation.startswith("sum of "):
        return "sum"
    return None


def assess_goal_conclusions(turn: TurnEvidence, entries: object,
                            goals: tuple[AnswerGoal, ...],
                            query_tools: dict[str, str]) -> tuple[str, ...]:
    """Return unfulfilled goals or wrong-scope metrics from model-selected evidence.

    This comparison intentionally does not compare display labels or model-written values.
    It follows computation input references to the typed query. Its read-tool identity is
    recorded by the server at dispatch, not inferred from adapter-authored origin text.
    """
    if not isinstance(entries, list):
        raise ValueError("goal-bound answer needs a conclusion list")
    required = {(goal.target_read_tool, measure)
                for goal in goals for measure in goal.measures}
    target_tools = {goal.target_read_tool for goal in goals}
    satisfied = set()
    wrong = []
    roots: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("evidence_ref"), str):
            continue  # Answer protocol supplies the detailed shape error.
        ref = entry["evidence_ref"]
        if entry.get("kind") == "selection":
            query = _query_root(turn, ref)
            if query is not None:
                tool = query_tools.get(query.evidence_id, "")
                if tool not in target_tools:
                    wrong.append(f"selection from {tool}")
            continue
        measure = _measure(turn, ref)
        if measure is None:
            continue
        query = _query_root(turn, ref)
        if query is None:
            continue
        tool = query_tools.get(query.evidence_id, "")
        key = (tool, measure)
        if key in required:
            prior = roots.setdefault(tool, query.evidence_id)
            if prior != query.evidence_id:
                wrong.append(f"inconsistent query scopes for {tool}")
            satisfied.add(key)
        else:
            wrong.append(f"{measure} from {tool}")
    missing = sorted(required - satisfied)
    return tuple([f"missing {measure} from {tool}" for tool, measure in missing]
                 + [f"unrequested {item}" for item in wrong])


def normalize_goal_entries(turn: TurnEvidence, entries: object,
                           goals: tuple[AnswerGoal, ...],
                           query_tools: dict[str, str],
                           generated_refs: frozenset[str], *,
                           complete: bool) -> tuple[list, tuple[str, ...]]:
    """Omit unrelated numeric detours; complete same-query metrics already computed.

    Only the model-selected requested query can nominate a generated sibling metric.
    A server-created number from a different subject/filter is never grafted onto it.
    The caller records every omission in audit before presenting the normalized answer.
    """
    if not isinstance(entries, list):
        raise ValueError("goal-bound answer needs a conclusion list")
    target_tools = {goal.target_read_tool for goal in goals}
    kept, removed = [], []
    chosen_roots: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("evidence_ref"), str):
            kept.append(entry)
            continue
        ref = entry["evidence_ref"]
        kind = entry.get("kind")
        if kind not in {"aggregate", "selection"}:
            kept.append(entry)
            continue
        root = _query_root(turn, ref)
        tool = query_tools.get(root.evidence_id, "") if root else ""
        if root is not None and tool not in target_tools:
            removed.append(ref)
            continue
        kept.append(entry)
        if kind == "aggregate" and _measure(turn, ref) is not None and root is not None:
            chosen_roots.setdefault(tool, root.evidence_id)
    if complete:
        for goal in goals:
            root_ref = chosen_roots.get(goal.target_read_tool)
            if not root_ref:
                continue
            for measure in sorted(goal.measures):
                if any(_measure(turn, item["evidence_ref"]) == measure
                       and (root := _query_root(turn, item["evidence_ref"])) is not None
                       and root.evidence_id == root_ref
                       for item in kept if isinstance(item, dict)
                       and isinstance(item.get("evidence_ref"), str)):
                    continue
                sibling = next((ref for ref in generated_refs
                                if _measure(turn, ref) == measure
                                and (root := _query_root(turn, ref)) is not None
                                and root.evidence_id == root_ref), None)
                if sibling:
                    kept.append({"kind": "aggregate", "evidence_ref": sibling})
    return kept, tuple(removed)


__all__ = ["AnswerGoal", "assess_goal_conclusions", "normalize_goal_entries",
           "parse_answer_goals"]
