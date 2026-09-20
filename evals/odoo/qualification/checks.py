"""Content-free checks over zero-model qualification results."""
from __future__ import annotations

import json
from typing import Any

from evals.odoo.domain import tasks
from evals.odoo.fixture.stack import REPOSITORY
from evals.odoo.provider.gateway import SCRIPTED_MODEL

def _selected(row: dict[str, Any]) -> dict[str, Any] | None:
    return row["attempts"][-1] if row["attempts"] else None


def evaluate_checks(result: dict[str, Any], batches: dict[str, Any]) -> dict[str, bool]:
    view = result["batches"]

    def rows(name: str) -> list[dict[str, Any]]:
        return view[name]["slots"]

    def outcome_is(row: dict[str, Any], expected: str) -> bool:
        attempt = _selected(row)
        return bool(attempt) and attempt["product_outcome"] == expected and attempt[
            "apparatus_integrity"] == "VALID"

    samples = rows("samples")
    strata = {task["task_id"]: task["stratum"] for task in
              json.loads((REPOSITORY / "evals/odoo/tasksets/p74-odoo-development-v1.json")
                         .read_text())["tasks"]}
    checks = {
        "split_and_seals": result["offline"]["split_matches_committed"],
        "leak_check": result["offline"]["leak_check"]["status"] == "READY",
        "stability_proof": result["offline"]["stability_proof"]["status"] == "READY",
        "reset_rejections": result["reset_probes"]["as_expected"],
        "samples_batch_complete": view["samples"]["status"] == "COMPLETE",
        "every_sample_passes": all(outcome_is(row, "PASS") for row in samples),
        "all_six_strata_on_woa": {strata[row["task_id"]] for row in samples
                                  if row["arm"] == "woa_product"} == set(tasks.STRATA),
        "pageagent_real_chromium_samples": sum(row["arm"] == "pageagent_browser_only"
                                               for row in samples) >= 3,
        "negative_controls_fail": all(outcome_is(row, "FAIL") for row in rows("negatives")),
        "provider_fault_replaced_then_cap_sealed": _fault_batch(view["faults_provider"],
                                                                "provider_unavailable"),
        "harness_fault_replaced_then_cap_sealed": _fault_batch(view["faults_harness"],
                                                               "evaluator_exception"),
        "prestart_fault_replaced_then_cap_sealed": _fault_batch(view["faults_prestart"],
                                                                "fixture_baseline_unstable"),
        "model_string_change_seals": view["seal_model"]["seal_reason"] == "model_identity_changed",
        "unowned_request_seals": view["seal_unowned"]["seal_reason"] == "unowned_model_request",
        "safety_event_seals": view["seal_safety"]["seal_reason"] == "critical_safety_event",
        "extra_side_effect_detected_and_sealed": (
            view["seal_side_effect"]["seal_reason"] == "critical_safety_event"
            and "extra_side_effect" in _selected(rows("seal_side_effect")[0])["safety_events"]),
        "interrupted_attempt_recovered_and_replaced": _interrupted_ok(view["interrupted"]),
        "turn_read_before_the_driver_died_is_judged": _settled_ok(view["settled_turn"]),
        "reset_failure_seals_without_erasing_product_result": (
            view["seal_reset"]["seal_reason"] == "reset_failed"
            and _selected(rows("seal_reset")[0])["product_outcome"] in {"PASS", "FAIL"}),
        "baseline_restored_after_reset_failure": result["post_reset_failure_restore"] == "PASS",
        "budget_refusal_is_product_failure": outcome_is(rows("budget")[0], "FAIL"),
        "domain_mutation_fails_evaluator": outcome_is(rows("domain_mutation")[0], "FAIL"),
        "rate_limit_replaced_on_woa": _replaced(view["typed_rate_limit"], "provider_rate_limited"),
        "rate_limit_replaced_on_pageagent": _replaced(view["typed_rate_limit_pageagent"],
                                                      "provider_rate_limited"),
        "response_contract_fault_replaced": _replaced(view["typed_response_contract"],
                                                      "provider_response_contract"),
        "context_rejection_scored_as_product": (
            view["typed_context_length"]["status"] == "COMPLETE"
            and outcome_is(rows("typed_context_length")[0], "FAIL")),
        "write_not_done_is_product_failure_not_a_safety_event": (
            view["write_not_done"]["status"] == "COMPLETE"
            and outcome_is(rows("write_not_done")[0], "FAIL")
            and _selected(rows("write_not_done")[0])["safety_events"] == []),
        **{f"deadline_answer_scored_as_product_on_{arm}": (
            view[name]["status"] == "COMPLETE" and len(rows(name)[0]["attempts"]) == 1
            and outcome_is(rows(name)[0], "FAIL"))
           for arm, name in (("woa", "typed_deadline"),
                             ("pageagent", "typed_deadline_pageagent"))},
        "refused_credential_seals": (
            view["typed_credential"]["seal_reason"] == "provider_authentication_failed"
            and _selected(rows("typed_credential")[0])["replacement_eligible"] is False),
        "request_outside_contract_seals_as_product_failure": (
            view["typed_request_contract"]["seal_reason"] == "request_contract_violation"
            and outcome_is(rows("typed_request_contract")[0], "FAIL")),
        "attempt_wall_stops_the_turn_and_is_scored": _wall_clock_ok(view["attempt_wall"],
                                                                   batches["attempt_wall"]),
        "no_unclassified_results": not any(
            attempt["unclassified"] for batch in view.values() for row in batch["slots"]
            for attempt in row["attempts"]),
        "report_recomputes_and_verifies": result.get("report_verified", False),
        "scripted_model_only": all(
            batch["summary"]["provider_model"] in {None, SCRIPTED_MODEL, "p74-scripted/v2"}
            for batch in batches.values()),
        "spend_ledgers_untouched": result["spend_ledgers_untouched"],
    }
    checks.update(feedback_checks(batches))
    return checks


def feedback_checks(batches: dict[str, Any]) -> dict[str, bool]:
    """Whether the panel acknowledged every submit and cancel within 300 ms, across the product
    arm's attempts: the part of the experience gate that measures the panel, not the model."""
    woa = [attempt["metrics"] for batch in batches.values()
           for attempt in batch["summary"]["attempts"]
           if attempt["arm"] == "woa_product" and attempt.get("metrics")]
    submit = [item.get("first_feedback_ms") for item in woa
              if item.get("first_feedback_ms") is not None]
    cancel = [item.get("cancel_feedback_ms") for item in woa
              if item.get("cancel_feedback_ms") is not None]
    return {"submit_feedback_within_300ms": bool(submit) and max(submit) <= 300,
            "cancel_feedback_within_300ms": bool(cancel) and max(cancel) <= 300}


def _replaced(batch: dict[str, Any], code: str) -> bool:
    attempts = batch["slots"][0]["attempts"]
    if batch["status"] != "COMPLETE" or len(attempts) != 2:
        return False
    original, replacement = attempts
    return (original["apparatus_code"] == code and original["replacement_eligible"] is True
            and replacement["replacement_of"] == original["attempt_id"]
            and replacement["product_outcome"] == "PASS"
            and replacement["apparatus_integrity"] == "VALID")


def _wall_clock_ok(view: dict[str, Any], batch: dict[str, Any]) -> bool:
    attempts = batch["summary"]["attempts"]
    if view["status"] != "COMPLETE" or len(attempts) != 1:
        return False
    (attempt,) = attempts
    return (attempt["product_outcome"] == "FAIL" and attempt["apparatus_integrity"] == "VALID"
            and (attempt.get("metrics") or {}).get("cancel_feedback_ms") is not None)


def _interrupted_ok(batch: dict[str, Any]) -> bool:
    first = batch["slots"][0]["attempts"]
    return (batch["status"] == "COMPLETE" and len(first) == 2
            and first[0]["apparatus_code"] == "browser_or_control_channel_crash"
            and first[0]["replacement_eligible"] is True
            and first[1]["replacement_of"] == first[0]["attempt_id"]
            and first[1]["product_outcome"] == "PASS")


def _settled_ok(batch: dict[str, Any]) -> bool:
    """The recovered attempt kept the failure its written-down turn showed, and was not
    replaced: a product failure seen before the driver died is the product's own."""
    first = batch["slots"][0]["attempts"]
    return (batch["status"] == "COMPLETE" and len(first) == 1
            and first[0]["apparatus_code"] == "browser_or_control_channel_crash"
            and first[0]["product_outcome"] == "FAIL")


def _fault_batch(batch: dict[str, Any], code: str) -> bool:
    first, second = batch["slots"]
    original = first["attempts"][0] if first["attempts"] else {}
    replacement = first["attempts"][1] if len(first["attempts"]) > 1 else {}
    return (original.get("apparatus_code") == code
            and original.get("product_outcome") == "NOT_EVALUABLE"
            and original.get("replacement_eligible") is True
            and replacement.get("product_outcome") == "PASS"
            and replacement.get("apparatus_integrity") == "VALID"
            and batch["seal_reason"] == "replacement_cap_exceeded"
            and len(second["attempts"]) == 1)


