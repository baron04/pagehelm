"""Application-neutral programmatic check specification and structured telemetry (ARC-12)."""
from __future__ import annotations

from dataclasses import dataclass

CHECK_OUTCOMES = frozenset({"returned", "annotated", "blocked"})


@dataclass(frozen=True, slots=True)
class ProgramCheckSpec:
    """Declarative specification for a programmatic turn or delivery check.

    Every programmatic guard/check declares what invariant it verifies, what
    correction instruction it returns to the model on retry, and what the user
    receives when two attempts still do not satisfy the check.
    """

    name: str
    checks_what: str
    retry_guidance: str
    terminal_delivery: str

    def __post_init__(self) -> None:
        for field_name in ("name", "checks_what", "retry_guidance", "terminal_delivery"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"ProgramCheckSpec.{field_name} must be a non-empty string")


def record_check_fired(
    ledger,
    *,
    s=None,
    trace=None,
    check: str,
    outcome: str,
    attempt: int,
    answer_preserved: bool,
) -> None:
    """Emit a uniform `check_fired` audit record and record into turn trace/context."""
    if not isinstance(check, str) or not check.strip():
        raise ValueError("check must be a non-empty string")
    if outcome not in CHECK_OUTCOMES:
        raise ValueError(f"outcome must be one of {sorted(CHECK_OUTCOMES)}")
    if type(attempt) is not int or attempt < 1:
        raise ValueError("attempt must be a positive integer")
    if type(answer_preserved) is not bool:
        raise ValueError("answer_preserved must be a bool")
    clean_check = check.strip()[:80]
    ledger.append(
        "check_fired",
        check=clean_check,
        outcome=outcome,
        attempt=attempt,
        answer_preserved=answer_preserved,
    )
    if s is not None:
        checks_list = getattr(s, "checks", None)
        if isinstance(checks_list, list):
            checks_list.append({
                "check": clean_check,
                "outcome": outcome,
                "attempt": attempt,
                "answer_preserved": answer_preserved,
            })
    if trace is not None and hasattr(trace, "record_check"):
        trace.record_check(
            check=clean_check,
            outcome=outcome,
            attempt=attempt,
            answer_preserved=answer_preserved,
        )


__all__ = ["CHECK_OUTCOMES", "ProgramCheckSpec", "record_check_fired"]
