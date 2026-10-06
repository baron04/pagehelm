"""A small request/answer surface compiled into the existing settlement contract.

References describe user text, never permissions. The ordinary evidence verifier,
action receipts and once-only approval still decide what can be reported or done.
"""
from copy import deepcopy
import re

from ..answer import answer_protocol
from ..answer.evidence import (CapabilityEvidence, ComputationEvidence, PageEvidence,
    QueryEvidence, RecordEvidence, SelectionEvidence, SkillEvidence, SourceEvidence,
    SourceHistoryEvidence)
from ..privacy import redact_sensitive_assignments
from .request_contract import ANSWER_KINDS, MAX_ANSWERS, TaskRequirements

SYSTEM = """
Use the registered tool schemas for this turn. Read-only data/source tools may run
before planning. To plan, assign the server's USER REQUEST references with plan_task;
do not copy or rewrite their text. Cover the entire request, including unsupported
parts. r0 is the whole request; use it for a single question, or use the clause refs
to separate questions and operations. Read the whole request when a clause has an
implicit subject. Instructions about how/why an operation works are knowledge
questions, not permission to perform it. A record detail/tab is record, form prefill
is draft, persisted change is mutation, general navigation is page.
Plan before page effects or completion. Correct a mistaken plan at most twice, using
the existing rules for committed effects; never discard requested work after effects.
finish is FLAT: outcome and conclusions. Each conclusion cites evidence_ref and the
current answer request_refs (a1 etc). For source/history/Skill supply statement and
an exact supporting span. Selection may specify fields. The server supplies the kind,
version, coverage and answer bindings; do not send answer/answers_for/done/why.
Page receipts cannot answer data/source questions. Partial/unable preserves observed
actions and verified facts and explicitly reports the unsettled clauses. All actions
remain subject to the application's policy, approval and postcondition checks.
"""


def request_references(prompt: str) -> dict[str, str]:
    text = redact_sensitive_assignments(prompt).strip()
    # Delimit punctuation and explicit sequential conjunctions, without trying to
    # infer entities, conditions or feasibility. The unsplit request stays r0.
    clauses = tuple(part.strip() for part in re.split(
        r"[，,；;。！？!?\n]+|(?=然后|同时|顺便|并且|并把|并打开|再把)", text)
                    if part.strip())
    return {"r0": text, **{f"r{i}": part for i, part in enumerate(clauses, 1)}}


def plan_tool(prompt: str) -> dict:
    refs = {"type": "array", "maxItems": 32, "uniqueItems": True,
            "items": {"type": "string", "enum": list(request_references(prompt))}}
    return {"type": "function", "function": {"name": "plan_task",
        "description": "Assign original USER REQUEST refs; do not retype text. This grants no authority. "
            "Use r0 for a question-only request; for mixed work preserve each clause and its implicit subject.",
        "parameters": {"type": "object", "additionalProperties": False,
            "properties": {"action_refs": refs,
                "action_kind": {"type": "string", "enum": ["none", "page", "record", "draft", "mutation"]},
                "answers": {"type": "array", "maxItems": MAX_ANSWERS, "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {"refs": {**refs, "minItems": 1},
                        "kind": {"type": "string", "enum": list(ANSWER_KINDS)}},
                    "required": ["refs", "kind"]}}},
            "required": ["action_refs", "action_kind", "answers"]}}}


def parse_plan(args: dict, prompt: str) -> TaskRequirements:
    if not isinstance(args, dict) or set(args) != {"action_refs", "action_kind", "answers"}:
        raise ValueError("plan_task takes action_refs, action_kind and answers only")
    refs = request_references(prompt)
    def selected(value):
        if (not isinstance(value, list) or len(value) > 32
                or any(not isinstance(ref, str) or ref not in refs for ref in value)
                or len(set(value)) != len(value)):
            raise ValueError("use unique server-issued original request refs")
        if "r0" in value and len(value) != 1:
            raise ValueError("r0 already covers the whole request; do not repeat its clauses")
        return value
    action = selected(args["action_refs"])
    kind = args["action_kind"]
    if (not isinstance(kind, str) or kind not in {"none", "page", "record", "draft", "mutation"}
            or bool(action) != (kind != "none")):
        raise ValueError("action_kind must match action_refs; use none for questions only")
    answers = args["answers"]
    if not isinstance(answers, list) or len(answers) > MAX_ANSWERS:
        raise ValueError("answers must be bounded")
    groups, kinds = [], []
    assigned = set(action)
    for answer in answers:
        if (not isinstance(answer, dict) or set(answer) != {"refs", "kind"}
                or not isinstance(answer["kind"], str) or answer["kind"] not in ANSWER_KINDS):
            raise ValueError("each answer takes refs and data/ui/knowledge kind")
        group = selected(answer["refs"])
        if not group or assigned.intersection(group):
            raise ValueError("assign each clause once; an operation is not an additional answer")
        assigned.update(group)
        groups.append(group)
        kinds.append(answer["kind"])
    if not assigned or not refs["r0"]:
        raise ValueError("plan must account for a nonempty original request")
    if "r0" in assigned:
        if len(assigned) != 1:
            raise ValueError("assign either the whole request or every separate clause")
    elif assigned != set(refs) - {"r0"}:
        raise ValueError("plan omitted original request clauses; include every ref")
    def text(group):
        # Retain source order even if the model lists refs out of order.
        return "，".join(refs[ref] for ref in refs if ref in group)
    return TaskRequirements(text(action), tuple(text(group) for group in groups), kind, tuple(kinds))


def finish_tool(session) -> dict:
    schema = answer_protocol.answer_schema(source=session.runtime_source,
                                          skill=bool(session._loaded_business_skill_ids))
    item = deepcopy(schema["properties"]["conclusions"]["items"])
    item["properties"].pop("kind")
    item["required"] = ["evidence_ref", "request_refs"]
    ids = session._task_requirements.answer_ids if session._task_requirements else ()
    item["properties"]["request_refs"] = {"type": "array", "maxItems": MAX_ANSWERS if ids else 0,
        "uniqueItems": True, "items": {"type": "string", **({"enum": list(ids)} if ids else {})},
        "description": "Current question IDs this conclusion answers; [] for an evidence gap."}
    return {"type": "function", "function": {"name": "finish",
        "description": "Report verified facts/actions. Cite references; the server builds the answer envelope. "
            "Source evidence needs statement and exact support. Unsettled work requires partial/unable.",
        "parameters": {"type": "object", "additionalProperties": False,
            "properties": {"outcome": {"type": "string", "enum": ["answered", "partial", "unable", "acted"]},
                "conclusions": {"type": "array", "maxItems": answer_protocol.MAX_CONCLUSIONS, "items": item},
                "presentation": schema["properties"]["presentation"]}, "required": ["outcome"]}}}


_KINDS = {RecordEvidence: "field_value", QueryEvidence: "selection", SelectionEvidence: "selection",
          ComputationEvidence: "aggregate", PageEvidence: "page_observation",
          CapabilityEvidence: "capability_gap", SourceEvidence: "source_excerpt",
          SourceHistoryEvidence: "source_history", SkillEvidence: "skill_statement"}


def compile_finish(args: dict, requirements: TaskRequirements, evidence) -> dict:
    if not isinstance(args, dict) or set(args) - {"outcome", "conclusions", "presentation"}:
        raise ValueError("finish takes outcome, conclusions and optional presentation; no answer wrapper")
    if (not isinstance(args.get("outcome"), str)
            or args["outcome"] not in {"answered", "partial", "unable", "acted"}):
        raise ValueError("invalid finish outcome")
    entries = args.get("conclusions", [])
    if not isinstance(entries, list) or len(entries) > answer_protocol.MAX_CONCLUSIONS:
        raise ValueError("invalid conclusions")
    conclusions, bindings = [], {}
    for entry in entries:
        if (not isinstance(entry, dict) or set(entry) - {
                "evidence_ref", "request_refs", "fields", "statement", "support"}
                or not {"evidence_ref", "request_refs"} <= set(entry)):
            raise ValueError("a conclusion needs evidence_ref and request_refs; source also needs statement/support")
        request_refs = entry["request_refs"]
        if (not isinstance(request_refs, list) or len(request_refs) > MAX_ANSWERS
                or any(not isinstance(ref, str) or ref not in requirements.answer_ids for ref in request_refs)
                or len(set(request_refs)) != len(request_refs)):
            raise ValueError("use only the current plan's unique answer IDs")
        ref = entry["evidence_ref"]
        if not isinstance(ref, str):
            raise ValueError("evidence_ref must be a string")
        item = evidence.get(ref)
        answer_kinds = dict(zip(requirements.answer_ids, requirements.answer_kinds))
        for request_ref in request_refs:
            if (answer_kinds[request_ref] == "data"
                    and isinstance(item, (SourceEvidence, SourceHistoryEvidence, SkillEvidence))):
                raise ValueError("static source/Skill evidence cannot answer a current business-data question")
            if (answer_kinds[request_ref] == "knowledge"
                    and isinstance(item, (RecordEvidence, QueryEvidence, SelectionEvidence, ComputationEvidence))):
                raise ValueError("business values cannot answer a source implementation question")
        kind = _KINDS.get(type(item))
        if kind is None:
            raise ValueError("this evidence cannot support a conclusion")
        if isinstance(item, RecordEvidence) and not item.field_was_read:
            kind = "undetermined"
        if isinstance(item, PageEvidence) and item.saw == "control_only":
            kind = "undetermined"
        if kind not in {"undetermined", "capability_gap"} and not request_refs:
            raise ValueError("a factual conclusion must bind a current question; [] is only for an evidence gap")
        conclusion = {key: value for key, value in entry.items() if key != "request_refs"}
        conclusions.append({**conclusion, "kind": kind})
        if kind not in {"undetermined", "capability_gap"}:
            for request_ref in request_refs:
                bindings.setdefault(request_ref, []).append(ref)
    result = {"outcome": args["outcome"]}
    if not conclusions and args["outcome"] == "partial":
        # No facts need verification. Existing settlement preserves the actual
        # action and marks the original unmet obligations as partial.
        result["outcome"] = "unable"
    if conclusions:
        result["answer"] = {"version": 1,
            "coverage": "complete" if args["outcome"] in {"answered", "acted"} else "partial",
            "conclusions": conclusions,
            "answers_for": [{"request_ref": ref, "evidence_refs": list(dict.fromkeys(values))}
                            for ref, values in bindings.items()]}
        if "presentation" in args:
            result["answer"]["presentation"] = args["presentation"]
    return result
