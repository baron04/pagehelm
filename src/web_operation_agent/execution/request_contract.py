"""Frozen user-request obligations, separate from application evidence or permissions.

This is completeness accounting, not semantic entailment. The model still has to
classify the request correctly and connect each question to the right evidence.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import re
import unicodedata

from ..privacy import redact_sensitive_assignments

MAX_ANSWERS = 8
DECLARE_TASK_TOOL = {"type": "function", "function": {
    "name": "declare_task",
    "description": (
        "Before any application read or action, account for the original USER REQUEST. "
        "Copy its operation clause into action_request (empty for questions only), and "
        "each independently requested answer clause into answer_requests. Preserve record "
        "names, requested fields, conditions and exclusions. Copy exact request text, "
        "including modifiers; do not decide feasibility or drop an unsupported part. "
        "Every request character except punctuation and connecting words must be covered. "
        "This immutable declaration grants no permission or tool. Runtime results decide "
        "what can be fulfilled; only evidence-backed answers can settle these questions."),
    "parameters": {"type": "object", "additionalProperties": False,
        "properties": {
            "action_request": {"type": "string", "maxLength": 4000},
            "answer_requests": {"type": "array", "maxItems": MAX_ANSWERS, "uniqueItems": True,
                "items": {"type": "string", "minLength": 1, "maxLength": 1000}}},
        "required": ["action_request", "answer_requests"]}}}

TASK_SYSTEM = """
First call declare_task for the USER REQUEST, before application reads or actions.
Separate reaching a page or preparing a draft from answering a question. Unknown or
unsupported questions still belong in the declaration; application metadata cannot
rewrite it. Its answer references freeze before results. In finish.answer.answers_for,
map each aN to the exact evidence_refs actually selected by that answer's conclusions.
A page/action receipt cannot replace a requested fact. Missing answers or an unfinished
operation require partial/unable, preserving any observed action and verified answers.
The declaration does not authorize writes or submission. Existing approval still applies.
"""

_CONNECTORS = re.compile(r"(?:(?:并且?|以及|和|然后|同时|顺便|还|再|也|请|帮我|麻烦)|"
                         r"\b(?:and|then|also|please)\b|\s)+", re.I)


@dataclass(frozen=True)
class TaskRequirements:
    action_request: str
    answer_requests: tuple[str, ...]

    @property
    def answer_ids(self) -> tuple[str, ...]:
        return tuple(f"a{i}" for i in range(1, len(self.answer_requests) + 1))

    def render(self) -> str:
        return "FROZEN USER REQUIREMENTS (not application authority):\n" + json.dumps({
            "action_request": self.action_request,
            "answers": dict(zip(self.answer_ids, self.answer_requests))}, ensure_ascii=False)


def parse_requirements(args: dict, prompt: str) -> TaskRequirements:
    if not isinstance(args, dict) or set(args) != {"action_request", "answer_requests"}:
        raise ValueError("declare_task requires action_request and answer_requests only")
    action, answers = args["action_request"], args["answer_requests"]
    if (not isinstance(action, str) or len(action) > 4000 or not isinstance(answers, list)
            or len(answers) > MAX_ANSWERS or any(not isinstance(q, str) or not q.strip()
                or len(q) > 1000 for q in answers)):
        raise ValueError("invalid action or answer request clauses")
    action, answers = action.strip(), tuple(q.strip() for q in answers)
    if len(set(answers)) != len(answers) or not (action or answers):
        raise ValueError("request clauses must be nonempty and questions must not repeat")
    prompt = redact_sensitive_assignments(prompt)
    covered = [False] * len(prompt)
    for clause in ((action,) if action else ()) + answers:
        start = prompt.find(clause)
        if start < 0:
            raise ValueError("each clause must quote exact original USER REQUEST text")
        while start >= 0:
            covered[start:start + len(clause)] = [True] * len(clause)
            start = prompt.find(clause, start + 1)
    # Detect a dropped clause without interpreting application nouns or field names.
    rest = "".join(char if not covered[i] and not unicodedata.category(char).startswith("P")
                   else " " for i, char in enumerate(prompt))
    if _CONNECTORS.sub("", rest).strip():
        raise ValueError("declaration omitted original request text; include every clause")
    return TaskRequirements(action, answers)


def finish_schema(tool: dict, requirements: TaskRequirements) -> dict:
    result = deepcopy(tool)
    payload = result["function"]["parameters"]["properties"]["answer"]
    payload["properties"]["answers_for"] = {"type": "array", "maxItems": MAX_ANSWERS,
        "description": "Bind each requested aN to evidence_refs selected by valid conclusions.",
        "items": {"type": "object", "additionalProperties": False,
            "properties": {
                "request_ref": {"type": "string", "enum": list(requirements.answer_ids)},
                "evidence_refs": {"type": "array", "minItems": 1, "maxItems": 12,
                    "uniqueItems": True, "items": {"type": "string", "minLength": 1, "maxLength": 100}}},
            "required": ["request_ref", "evidence_refs"]}}
    return result


def extract_answer_bindings(args: dict) -> tuple[dict, object]:
    answer = args.get("answer") if isinstance(args, dict) else None
    if not isinstance(answer, dict):
        return args, []
    bindings = answer.get("answers_for", [])
    return {**args, "answer": {key: value for key, value in answer.items()
                              if key != "answers_for"}}, bindings


def missing_answers(requirements: TaskRequirements, bindings: object, conclusions) -> tuple[str, ...]:
    if not isinstance(bindings, list) or len(bindings) > MAX_ANSWERS:
        raise ValueError("answers_for must be a bounded array")
    answered, used = set(), {entry.get("evidence_ref") for entry in conclusions or ()
                            if isinstance(entry, dict) and isinstance(entry.get("evidence_ref"), str)
                            and entry.get("kind") not in {
                                "undetermined", "capability_gap"}}
    for binding in bindings:
        if not isinstance(binding, dict) or set(binding) != {"request_ref", "evidence_refs"}:
            raise ValueError("answers_for entries take request_ref and evidence_refs only")
        ref, evidence = binding["request_ref"], binding["evidence_refs"]
        if not isinstance(ref, str) or ref not in requirements.answer_ids or ref in answered:
            raise ValueError("answers_for contains an unknown or repeated request_ref")
        if (not isinstance(evidence, list) or not 1 <= len(evidence) <= 12
                or any(not isinstance(item, str) or item not in used for item in evidence)
                or len(set(evidence)) != len(evidence)):
            raise ValueError("answers_for must cite references actually selected by answer conclusions")
        answered.add(ref)
    return tuple(ref for ref in requirements.answer_ids if ref not in answered)


def incomplete_notes(requirements, missing, *, action_missing: bool, chinese: bool):
    answers = dict(zip(requirements.answer_ids, requirements.answer_requests))
    return tuple(("未完成问答：" if chinese else "Unanswered request: ") + answers[ref] for ref in missing) + (
        (("未完成操作：" if chinese else "Unfinished operation: ") + requirements.action_request,)
        if action_missing else ())
