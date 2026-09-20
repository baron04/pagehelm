"""Validation of the privacy-safe public evaluation report."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _forbidden_path(value: Any, forbidden_keys: frozenset[str]) -> str | None:
    stack: list[tuple[Any, str]] = [(value, "$")]
    while stack:
        item, path = stack.pop()
        if isinstance(item, Mapping):
            for key, child in item.items():
                if isinstance(key, str) and key.lower() in forbidden_keys:
                    return f"{path}.{key}"
                stack.append((child, f"{path}.{key}"))
        elif isinstance(item, (list, tuple)):
            stack.extend((child, f"{path}[{index}]") for index, child in enumerate(item))
    return None


def verify_report(value: Any) -> dict[str, Any]:
    # Lazy import avoids making the validation module the owner of report schema constants.
    from evals.odoo.reporting import report as schema

    if (not isinstance(value, Mapping)
            or value.get("artifact_version") != schema.REPORT_VERSION
            or value.get("kind") != schema.REPORT_KIND):
        raise ValueError("public report header is invalid")
    if set(value) != schema._REPORT_FIELDS:
        raise ValueError("public report has unexpected fields")
    claims = value["claims"]
    if (not isinstance(claims, Mapping) or set(claims) != set(schema.CLAIMS)
            or any(claims[name] is not False for name in schema.CLAIMS)):
        raise ValueError("public report claims must all be false")
    if value["limitations"] != list(schema.LIMITATIONS):
        raise ValueError("public report must state every fixed limitation")
    path = _forbidden_path(value, schema.FORBIDDEN_KEYS)
    if path is not None:
        raise ValueError(f"public report publishes a forbidden field at {path}")
    if not isinstance(value["batches"], list) or not isinstance(value["safety"], Mapping):
        raise ValueError("public report batches or safety block is malformed")
    if any(not isinstance(item, Mapping) or (item.get("verdict_status") == "gated"
                                             and item.get("verdict_basis") != [])
           for item in value["batches"]):
        raise ValueError("a gated batch cannot carry a reason it may not be gated")
    comparisons = value["comparisons"]
    if not isinstance(comparisons, list) or any(
            not isinstance(item, Mapping)
            or item.get("interpretation") != schema.COMPARISON_INTERPRETATION
            or item.get("causal_engine_attribution") is not False
            or item.get("non_inferiority_claim") is not False
            or item.get("scope") != schema.COMPARISON_SCOPE
            for item in comparisons):
        raise ValueError("public report comparisons must stay exploratory and pair only tasks "
                         "judged by their final page")
    answers = value["answer_task_results"]
    if not isinstance(answers, list) or any(
            not isinstance(item, Mapping) or item.get("scope") != schema.ANSWER_SCOPE
            or item.get("paired") is not False
            or item.get("answer_visible_to_user") != {
                schema.GATED_ARM: True, schema.COMPARISON_ARM: False}
            or {"difference", "newcombe_low", "newcombe_high", "exact_mcnemar_p"} & set(item)
            for item in answers):
        raise ValueError("answer-task results are counted per arm, never paired, and say that "
                         "PageAgent's answer is not shown to the user")
    return {"artifact_version": schema.REPORT_VERSION,
            "kind": "p74_public_report_verification", "status": "READY",
            "batches": len(value["batches"]), "comparisons": len(comparisons),
            "safety_gate_failed": value["safety"].get("safety_gate_failed")}
