"""Knowledge selection, independent of business declaration and write authority."""
from __future__ import annotations

from jsonschema import Draft202012Validator


SELECTION_INSTRUCTIONS = """
Choose select_skills for a request to perform an operation covered by an installed
Skill, including a mixed request with later reads/pages. Select by the package's
description, never by an exact phrase or a keyword list. Return only installed
identities; do not extract fields, values, records or business steps here. The next
Agent decision reads the COMPLETE original request and loaded instructions.
Queries, page requests, preparation, negation, explanations and source questions
use declare_scope and its existing schema. An installed write Skill does not turn
a question into execution. If no installed Skill fits, use declare_scope normally.
You may call select_skills and declare_scope together when a scope is needed;
each call is independently validated. The scope supplies lookup hints, not a
second plan or write authority. For a repository question, declare_scope preserves
that question and any live fact needed for a source comparison. Do not repeat
supporting lookups as user deliverables. Selection loads knowledge only; it never
authorizes a write.
"""


def selection_tool(candidates):
    return {"type": "function", "function": {
        "name": "select_skills",
        "description": "Select installed knowledge for requested business execution; no values, plan or authority.",
        "parameters": {"type": "object", "properties": {
            "skill_ids": {"type": "array", "minItems": 1, "maxItems": 4,
                "uniqueItems": True, "items": {"type": "string",
                    "enum": [s["skill_id"] for s in candidates]}}},
            "required": ["skill_ids"], "additionalProperties": False}}}


def selected_ids(payload, candidates):
    validator = Draft202012Validator(selection_tool(candidates)["function"]["parameters"])
    errors = list(validator.iter_errors(payload))
    if errors:
        # No rejected values or untrusted instruction text enter the repair.
        error = errors[0]
        path = ".".join(str(p) for p in error.absolute_path) or "selection"
        raise ValueError(f"Skill selection at {path} violates {error.validator}")
    return tuple(payload["skill_ids"])
