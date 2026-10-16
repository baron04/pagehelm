"""Bind application-issued navigation targets to the answer that actually used them."""
from __future__ import annotations

from dataclasses import dataclass

from .model_loop_contracts import AnswerTarget


@dataclass(frozen=True, slots=True)
class BoundAnswerTarget:
    read_ref: str
    target: AnswerTarget

    def __post_init__(self):
        if (not isinstance(self.read_ref, str) or not self.read_ref.startswith("read#")
                or not self.read_ref[5:].isdigit() or len(self.read_ref) > 20
                or not isinstance(self.target, AnswerTarget)):
            raise ValueError("invalid bound answer target")

    def to_json(self) -> dict:
        return {"kind": self.target.kind, "address": self.target.address,
                "label": self.target.label, "identity": self.target.identity,
                "read_ref": self.read_ref}


def answer_targets_for(issued: tuple[BoundAnswerTarget, ...], answer: str, *,
                       finished: bool) -> tuple[BoundAnswerTarget, ...]:
    """Only surface a target named in a completed answer and one exact read identity.

    The answer's words only narrow already-issued targets; they never create an address.
    Same-label targets remain inert because a label alone cannot choose between them.
    """
    if not finished or not isinstance(answer, str):
        return ()
    labels: dict[str, set[str]] = {}
    for entry in issued:
        labels.setdefault(entry.target.label, set()).add(entry.target.identity)
    selected, seen = [], set()
    for entry in issued:
        target = entry.target
        if (target.label not in answer or len(labels[target.label]) != 1
                or target.identity in seen):
            continue
        selected.append(entry)
        seen.add(target.identity)
        if len(selected) >= 4:
            break
    return tuple(selected)
