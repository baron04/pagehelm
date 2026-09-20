"""Exact small-sample statistics for the Odoo evaluation protocol.

Suites here have tens of tasks per arm, where normal approximations misstate tail
probabilities and collapse intervals at 0% or 100%. Binomial quantities are therefore
summed exactly in integers, and every input is validated before a number reaches a report.
"""
from __future__ import annotations

import math
from fractions import Fraction
from statistics import NormalDist
from typing import Any, Sequence


_BISECTION_TOLERANCE = 1e-10


def _count(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}")
    return value


def _within(count: Any, n: Any, name: str) -> tuple[int, int]:
    total = _count(n, "n", minimum=1)
    selected = _count(count, name)
    if selected > total:
        raise ValueError(f"{name} cannot exceed n")
    return selected, total


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    selected = float(value)
    if not math.isfinite(selected):
        raise ValueError(f"{name} must be finite")
    return selected


def _probability(value: Any, name: str) -> float:
    selected = _finite(value, name)
    if not 0 <= selected <= 1:
        raise ValueError(f"{name} must be between zero and one")
    return selected


def _confidence(value: Any) -> float:
    selected = _finite(value, "confidence")
    if not 0 < selected < 1:
        raise ValueError("confidence must be strictly between zero and one")
    return selected


def _sequence(value: Any, name: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{name} must be a sequence")
    return value


def _flags(value: Any, name: str) -> list[bool]:
    flags = list(_sequence(value, name))
    if not all(isinstance(flag, bool) for flag in flags):
        raise ValueError(f"{name} must contain only booleans")
    return flags


def _z(confidence: float) -> float:
    return NormalDist().inv_cdf(1 - (1 - confidence) / 2)


def _binomial_probability(n: int, p: float, low: int, high: int) -> float:
    """Sum P[low <= X <= high] as one integer ratio so per-term rounding cannot accumulate."""
    numerator, denominator = p.as_integer_ratio()
    complement = denominator - numerator
    total = sum(math.comb(n, k) * numerator ** k * complement ** (n - k)
                for k in range(low, high + 1))
    return total / denominator ** n


def screening_threshold(n: int) -> int:
    """Return ceil(0.9 * n) in integer arithmetic so binary error in 0.9 never moves the bar."""
    total = _count(n, "n", minimum=1)
    return -(-9 * total // 10)


def wilson_interval(successes: int, n: int,
                    confidence: float = 0.95) -> tuple[float, float]:
    """Return the two-sided Wilson score interval for a binomial rate.

    Unlike the Wald interval it stays inside [0, 1] and keeps nonzero width at 0/n and
    n/n, which is exactly where a small suite is most uncertain.
    """
    hits, total = _within(successes, n, "successes")
    z = _z(_confidence(confidence))
    rate = hits / total
    z2 = z * z
    scale = 1 + z2 / total
    centre = (rate + z2 / (2 * total)) / scale
    half = z / scale * math.sqrt(rate * (1 - rate) / total + z2 / (4 * total * total))
    low = 0.0 if hits == 0 else max(0.0, centre - half)
    high = 1.0 if hits == total else min(1.0, centre + half)
    return low, high


def binomial_upper_tail(n: int, threshold: int, p: float) -> float:
    """Return P[X >= threshold] for X ~ Binomial(n, p) as an exact sum.

    Screening pass probabilities live in the tail where approximations are least
    trustworthy, so every term is exact until the single final division.
    """
    bar, total = _within(threshold, n, "threshold")
    return _binomial_probability(total, _probability(p, "p"), bar, total)


def operating_characteristics(n: int, true_rates: Sequence[float]) -> list[dict[str, Any]]:
    """Tabulate how often an agent at each true rate clears the 90% screening bar.

    Publishing this next to the bar shows how often a suite of size n misclassifies a
    genuinely good or genuinely weak agent.
    """
    total = _count(n, "n", minimum=1)
    rates = [_probability(rate, "true_rate") for rate in _sequence(true_rates, "true_rates")]
    if not rates:
        raise ValueError("at least one true rate is required")
    threshold = screening_threshold(total)
    return [{
        "true_rate": rate,
        "threshold": threshold,
        "pass_probability": round(binomial_upper_tail(total, threshold, rate), 3),
    } for rate in rates]


def zero_event_upper_bound(n: int, confidence: float = 0.95) -> float:
    """Return the exact one-sided upper bound on a rate after 0 events in n trials.

    A clean run does not show zero risk; this is the largest rate still consistent with
    seeing no events (about 3/n at 95%, the rule of three).
    """
    total = _count(n, "n", minimum=1)
    level = _confidence(confidence)
    return 1 - (1 - level) ** (1 / total)


def one_sided_upper_bound(events: int, n: int, confidence: float = 0.95) -> float:
    """Return the exact one-sided Clopper-Pearson upper bound for events in n trials.

    Bisection on the exact binomial CDF avoids a beta-quantile dependency. The upper end
    of the final bracket is returned so the bound is never below the exact root.
    """
    hits, total = _within(events, n, "events")
    level = _confidence(confidence)
    if hits == total:
        return 1.0
    if hits == 0:
        return zero_event_upper_bound(total, level)
    alpha = 1 - level
    low, high = 0.0, 1.0
    while high - low > _BISECTION_TOLERANCE:
        middle = (low + high) / 2
        if _binomial_probability(total, middle, 0, hits) > alpha:
            low = middle
        else:
            high = middle
    return high


def exact_mcnemar_p(b: int, c: int) -> float:
    """Return the two-sided exact McNemar p-value from the discordant pair counts.

    Only discordant pairs inform a paired difference, and with few of them the chi-square
    form is unreliable, so the conditional Binomial(b + c, 1/2) is used directly.
    """
    first = _count(b, "b")
    second = _count(c, "c")
    discordant = first + second
    if discordant == 0:
        return 1.0
    lower_tail = sum(math.comb(discordant, k) for k in range(min(first, second) + 1))
    return min(1.0, 2 * lower_tail / 2 ** discordant)


def _newcombe_phi(a: int, b: int, c: int, d: int) -> float:
    """Return the table's phi with Newcombe's continuity correction for positive association."""
    margins = (a + b) * (c + d) * (a + c) * (b + d)
    if margins == 0:
        return 0.0
    cross: float = a * d - b * c
    if cross > 0:
        cross = max(cross - (a + b + c + d) / 2, 0)
    return cross / math.sqrt(margins)


def newcombe_paired_difference_interval(
        a: int, b: int, c: int, d: int,
        confidence: float = 0.95) -> tuple[float, float, float]:
    """Return (p1 - p2, lower, upper) by Newcombe's (1998) method 10 for paired proportions.

    The table is a = both pass, b = arm 1 only, c = arm 2 only, d = both fail. Each arm's
    Wilson limits are combined with the within-task correlation, so an all-concordant
    table still gets a nonzero-width interval instead of a falsely certain [0, 0].
    """
    both = _count(a, "a")
    first_only = _count(b, "b")
    second_only = _count(c, "c")
    neither = _count(d, "d")
    total = both + first_only + second_only + neither
    if total == 0:
        raise ValueError("the paired table must contain at least one pair")
    level = _confidence(confidence)
    p1 = (both + first_only) / total
    p2 = (both + second_only) / total
    l1, u1 = wilson_interval(both + first_only, total, level)
    l2, u2 = wilson_interval(both + second_only, total, level)
    phi = _newcombe_phi(both, first_only, second_only, neither)
    difference = (first_only - second_only) / total
    lower = difference - math.sqrt(max(
        0.0, (p1 - l1) ** 2 - 2 * phi * (p1 - l1) * (u2 - p2) + (u2 - p2) ** 2))
    upper = difference + math.sqrt(max(
        0.0, (p2 - l2) ** 2 - 2 * phi * (p2 - l2) * (u1 - p1) + (u1 - p1) ** 2))
    return difference, max(-1.0, lower), min(1.0, upper)


def nearest_rank_percentile(values: Sequence[float], q: float) -> float:
    """Return the nearest-rank percentile, which is always an observed value.

    Budgets are judged against real observations, so nothing is interpolated. q is read
    through its shortest decimal form so that 95 means exactly 95/100 of the ranks.
    """
    numbers = sorted(_finite(value, "value") for value in _sequence(values, "values"))
    if not numbers:
        raise ValueError("values must not be empty")
    level = _finite(q, "q")
    if not 0 < level <= 100:
        raise ValueError("q must be greater than 0 and at most 100")
    rank = math.ceil(Fraction(repr(level)) * len(numbers) / 100)
    return numbers[rank - 1]


def paired_transitions(primary: Sequence[bool],
                       replicate: Sequence[bool]) -> dict[str, Any]:
    """Count task-level agreement between a primary run and its replicate.

    Replicates of one task are not independent units, so they are compared pair by pair
    instead of being pooled into a larger denominator.
    """
    first = _flags(primary, "primary")
    second = _flags(replicate, "replicate")
    if len(first) != len(second):
        raise ValueError("primary and replicate must cover the same tasks")
    if not first:
        raise ValueError("at least one paired observation is required")
    pairs = list(zip(first, second))
    counts = {
        "pass_pass": sum(1 for x, y in pairs if x and y),
        "pass_fail": sum(1 for x, y in pairs if x and not y),
        "fail_pass": sum(1 for x, y in pairs if not x and y),
        "fail_fail": sum(1 for x, y in pairs if not x and not y),
    }
    agreement = counts["pass_pass"] + counts["fail_fail"]
    return {**counts, "agreement": agreement, "n": len(pairs),
            "agreement_rate": agreement / len(pairs)}


def proportion_summary(successes: int, n: int,
                       confidence: float = 0.95) -> dict[str, Any]:
    """Summarise a rate with its Wilson interval, keeping an empty denominator explicit.

    A 0-of-0 cell shown as 0% would read as a measured failure, so it is reported as None.
    """
    total = _count(n, "n")
    level = _confidence(confidence)
    if total == 0:
        if _count(successes, "successes") != 0:
            raise ValueError("successes cannot exceed n")
        return {"successes": 0, "n": 0, "rate": None,
                "wilson_low": None, "wilson_high": None}
    hits, total = _within(successes, total, "successes")
    low, high = wilson_interval(hits, total, level)
    return {"successes": hits, "n": total, "rate": round(hits / total, 6),
            "wilson_low": round(low, 6), "wilson_high": round(high, 6)}
