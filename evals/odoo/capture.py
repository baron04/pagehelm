"""Every fallible observation this apparatus makes, with the source and scope it came from.

A component the apparatus could not read is not the same fact as a component it read as empty.
The six blocking defects the reviews of 1a0d390 through b0fffbe found were all one mistake:
an unread audit became "nothing was dispatched", an unread guard became "nothing changed", a
turn with no recorded end became "this turn made every request". Those are different facts and
they now have different types, different JSON and different handling.

:class:`Present` carries what was read, where it came from and whether the source could show the
read was complete; :class:`Unavailable` carries why nothing was read. There is deliberately no
``value_or(default)`` helper: a consumer must branch on the two cases, and a consumer that
forgets raises :class:`Unobserved` instead of scoring a fact nobody observed.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

#: Every fallible source. An observation belongs to exactly one of them, and a source is split
#: no more coarsely than it can fail: a call that returns while the next one raises is still a
#: source of its own, or its result would be thrown away with the failure of another.
SOURCES = frozenset({
    "runner_artifact",      # the arm's own record of the run
    "final_page",           # the page as it stood after the last turn
    "agent_audit",          # the product's audit of what it did
    "agent_state",          # the Agent's state once the attempt logged out
    "fixture_guard",        # the fixture as it stood after the run
    "evaluator_output",     # the separate evaluator process's judgement
    "provider_settlement",  # what the gateway settled of each model call. This one is carried
                            # by the ledger rather than by a capture: it must outlive the
                            # process, so an answer, a refusal and a call nobody heard back
                            # about are three entry kinds instead of two in-memory shapes
})


class Unobserved(LookupError):
    """A consumer asked for the value of something the apparatus never observed."""


@dataclass(frozen=True, slots=True)
class Present:
    """What one source returned, with the scope it belongs to.

    ``complete`` is false when the source answered but could not show that the answer holds
    everything it should: a truncated audit answers, and answering is not the same as being
    whole. A consumer that needs completeness must check it; nothing infers it from the value.
    """

    value: Any
    source: str
    scope: dict[str, Any]
    complete: bool = True

    def __post_init__(self) -> None:
        if self.source not in SOURCES:
            raise ValueError(f"{self.source} is not a fallible source of this apparatus")

    @property
    def available(self) -> bool:
        return True

    def require(self, what: str = "") -> Any:
        return self.value


@dataclass(frozen=True, slots=True)
class Unavailable:
    """Why one source gave nothing, in the scope it would have belonged to."""

    reason: str
    source: str
    scope: dict[str, Any]

    def __post_init__(self) -> None:
        if self.source not in SOURCES:
            raise ValueError(f"{self.source} is not a fallible source of this apparatus")

    @property
    def available(self) -> bool:
        return False

    @property
    def complete(self) -> bool:
        return False

    def require(self, what: str = "") -> Any:
        raise Unobserved(f"{what or self.source} was not observed: {self.reason}")


Capture = Present | Unavailable


def read(source: str, scope: dict[str, Any], producer: Callable[[], Any], *,
         errors: tuple[type[BaseException], ...] = (Exception,),
         complete: Callable[[Any], bool] = lambda _value: True) -> Capture:
    """Read one source into a capture, so a failure of another source cannot discard it."""
    try:
        value = producer()
    except errors as exc:
        return Unavailable(type(exc).__name__, source, dict(scope))
    return Present(value, source, dict(scope), complete=bool(complete(value)))


def as_record(capture: Capture) -> dict[str, Any]:
    """The capture as it is written to evidence: an empty value and an unread source keep
    different shapes through a round trip, which is what the evaluator and the ledger read."""
    if capture.available:
        return {"status": "present", "source": capture.source, "scope": dict(capture.scope),
                "complete": capture.complete, "value": capture.value}
    return {"status": "unavailable", "source": capture.source, "scope": dict(capture.scope),
            "reason": capture.reason}


#: The two record shapes, written whole by :func:`as_record`. A record missing one of these
#: fields is not a capture with a default for it -- it is a record written by something else.
_PRESENT = {"status", "source", "scope", "complete", "value"}
_UNAVAILABLE = {"status", "source", "scope", "reason"}


def of_record(record: Any) -> Capture:
    """The capture a record holds. A record of any other shape is refused rather than guessed."""
    if (not isinstance(record, dict) or record.get("source") not in SOURCES
            or not isinstance(record.get("scope"), dict)):
        raise ValueError("not an observation record")
    if record.get("status") == "present" and set(record) == _PRESENT:
        if not isinstance(record["complete"], bool):
            raise ValueError("not an observation record")
        return Present(record["value"], record["source"], dict(record["scope"]),
                       complete=record["complete"])
    if record.get("status") == "unavailable" and set(record) == _UNAVAILABLE:
        if not isinstance(record["reason"], str) or not record["reason"]:
            raise ValueError("not an observation record")
        return Unavailable(record["reason"], record["source"], dict(record["scope"]))
    raise ValueError("not an observation record")
