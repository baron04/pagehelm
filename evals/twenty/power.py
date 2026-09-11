"""Transparent planning calculations for paired binary benchmark outcomes.

This module plans independent workflow-template counts. Prompt paraphrases and repeated
runs of the same workflow are deliberately not accepted as extra independent units. The
calculation is a normal approximation for the paired success difference and uses a
Bonferroni per-hypothesis alpha, which is conservative for the preregistered Holm family.
It is a sensitivity analysis until a paired development pilot supplies discordance data.
"""
from __future__ import annotations

import math
from statistics import NormalDist
from typing import Any, Iterable

from .evaluator import EvaluationInputError, digest_json


POWER_VERSION = 1
_METHOD = "paired_difference_normal_approximation"
_MULTIPLICITY = "bonferroni_conservative_for_holm"
_INTERPRETATION = (
    "This is an assumptions-only sensitivity analysis over independent paired workflow "
    "templates. It is not an observed model result. Rephrasings and repeated runs do not "
    "increase the independent sample size. A formal count must be recomputed from a paired "
    "development pilot before the held-out suite is sealed."
)


def _probability(value: Any, name: str, *, inclusive_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvaluationInputError(f"{name} must be a probability")
    selected = float(value)
    lower_ok = selected >= 0 if inclusive_zero else selected > 0
    if not math.isfinite(selected) or not lower_ok or selected >= 1:
        raise EvaluationInputError(f"{name} must be between zero and one")
    return selected


def _positive_integer(value: Any, name: str, *, maximum: int = 1_000_000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise EvaluationInputError(f"{name} must be a positive integer")
    return value


def _required_pairs(*, discordance: float, null_difference: float,
                    alternative_difference: float, alpha: float,
                    target_power: float) -> int:
    effect = alternative_difference - null_difference
    if effect <= 0:
        raise EvaluationInputError("the alternative must exceed the null boundary")
    if discordance < max(abs(null_difference), abs(alternative_difference)):
        raise EvaluationInputError("discordance cannot be smaller than a paired difference")
    null_variance = discordance - null_difference ** 2
    alternative_variance = discordance - alternative_difference ** 2
    z_alpha = NormalDist().inv_cdf(1 - alpha)
    z_power = NormalDist().inv_cdf(target_power)
    numerator = (z_alpha * math.sqrt(max(0.0, null_variance))
                 + z_power * math.sqrt(max(0.0, alternative_variance)))
    return max(1, math.ceil((numerator / effect) ** 2))


def _power_at(*, pairs: int, discordance: float, null_difference: float,
              alternative_difference: float, alpha: float) -> float:
    effect = alternative_difference - null_difference
    null_variance = discordance - null_difference ** 2
    alternative_variance = discordance - alternative_difference ** 2
    if alternative_variance <= 0:
        return 1.0 if effect * math.sqrt(pairs) > (
            NormalDist().inv_cdf(1 - alpha) * math.sqrt(max(0.0, null_variance))) else 0.0
    score = ((effect * math.sqrt(pairs)
              - NormalDist().inv_cdf(1 - alpha) * math.sqrt(max(0.0, null_variance)))
             / math.sqrt(alternative_variance))
    return NormalDist().cdf(score)


def build_power_sensitivity(*, discordance_rates: Iterable[float],
                            familywise_alpha: float = 0.05,
                            primary_hypotheses: int = 3,
                            target_power: float = 0.80,
                            superiority_effect_pp: int = 10,
                            noninferiority_margin_pp: int = 5,
                            planned_pairs: int = 90) -> dict[str, Any]:
    """Build a digest-bound sensitivity artifact without consuming benchmark outcomes."""
    family_alpha = _probability(familywise_alpha, "familywise_alpha")
    hypotheses = _positive_integer(primary_hypotheses, "primary_hypotheses", maximum=20)
    desired_power = _probability(target_power, "target_power")
    planned = _positive_integer(planned_pairs, "planned_pairs")
    superiority_pp = _positive_integer(
        superiority_effect_pp, "superiority_effect_pp", maximum=50)
    noninferiority_pp = _positive_integer(
        noninferiority_margin_pp, "noninferiority_margin_pp", maximum=20)
    superiority = superiority_pp / 100
    margin = noninferiority_pp / 100
    per_hypothesis_alpha = family_alpha / hypotheses
    rates = sorted({_probability(rate, "discordance_rate")
                    for rate in discordance_rates})
    if not rates:
        raise EvaluationInputError("at least one discordance scenario is required")

    scenarios = []
    for discordance in rates:
        if discordance < max(superiority, margin):
            raise EvaluationInputError(
                "discordance scenarios must support both declared paired differences")
        superiority_pairs = _required_pairs(
            discordance=discordance, null_difference=0,
            alternative_difference=superiority, alpha=per_hypothesis_alpha,
            target_power=desired_power)
        noninferiority_pairs = _required_pairs(
            discordance=discordance, null_difference=-margin,
            alternative_difference=0, alpha=per_hypothesis_alpha,
            target_power=desired_power)
        superiority_power = _power_at(
            pairs=planned, discordance=discordance, null_difference=0,
            alternative_difference=superiority, alpha=per_hypothesis_alpha)
        noninferiority_power = _power_at(
            pairs=planned, discordance=discordance, null_difference=-margin,
            alternative_difference=0, alpha=per_hypothesis_alpha)
        scenarios.append({
            "discordance_rate": round(discordance, 6),
            "superiority_required_pairs": superiority_pairs,
            "superiority_power_at_planned": round(superiority_power, 6),
            "planned_supports_superiority": superiority_power >= desired_power,
            "noninferiority_required_pairs": noninferiority_pairs,
            "noninferiority_power_at_planned": round(noninferiority_power, 6),
            "planned_supports_noninferiority": noninferiority_power >= desired_power,
        })
    core = {
        "artifact_version": POWER_VERSION,
        "kind": "paired_binary_power_sensitivity",
        "status": "READY",
        "method": _METHOD,
        "multiplicity": _MULTIPLICITY,
        "familywise_alpha": family_alpha,
        "per_hypothesis_alpha": round(per_hypothesis_alpha, 12),
        "primary_hypotheses": hypotheses,
        "target_power": desired_power,
        "superiority_effect_pp": superiority_pp,
        "noninferiority_margin_pp": noninferiority_pp,
        "planned_pairs": planned,
        "scenarios": scenarios,
        "interpretation": _INTERPRETATION,
    }
    return {**core, "plan_digest": digest_json(core)}


def verify_power_sensitivity(value: Any) -> dict[str, Any]:
    """Recompute every result and reject edited or relabelled planning artifacts."""
    fields = {
        "artifact_version", "kind", "status", "method", "multiplicity",
        "familywise_alpha", "per_hypothesis_alpha", "primary_hypotheses",
        "target_power", "superiority_effect_pp", "noninferiority_margin_pp",
        "planned_pairs", "scenarios", "interpretation", "plan_digest",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise EvaluationInputError("power sensitivity artifact has unexpected fields")
    if (value.get("artifact_version") != POWER_VERSION
            or value.get("kind") != "paired_binary_power_sensitivity"
            or value.get("status") != "READY" or value.get("method") != _METHOD
            or value.get("multiplicity") != _MULTIPLICITY
            or value.get("interpretation") != _INTERPRETATION):
        raise EvaluationInputError("power sensitivity artifact header is invalid")
    scenarios = value.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise EvaluationInputError("power sensitivity scenarios are absent")
    expected = build_power_sensitivity(
        discordance_rates=[row.get("discordance_rate") for row in scenarios
                           if isinstance(row, dict)],
        familywise_alpha=value.get("familywise_alpha"),
        primary_hypotheses=value.get("primary_hypotheses"),
        target_power=value.get("target_power"),
        superiority_effect_pp=value.get("superiority_effect_pp"),
        noninferiority_margin_pp=value.get("noninferiority_margin_pp"),
        planned_pairs=value.get("planned_pairs"),
    )
    if value != expected:
        raise EvaluationInputError("power sensitivity artifact does not recompute")
    return {
        "artifact_version": POWER_VERSION,
        "kind": "paired_binary_power_sensitivity_verification",
        "status": "READY",
        "scenario_count": len(expected["scenarios"]),
        "plan_digest": expected["plan_digest"],
    }
