"""The approval oracle: the synthetic user who decides every write confirmation.

It sees what a person sees -- the effect and targets on the product's confirmation card --
and, as a check a person cannot make, the server's own summary of the pending proposal. It
approves only when all three agree exactly: the visible card, the server proposal and the
preregistered effect and targets of the task. A multi-target mismatch, a partial match, a
second pending proposal or a card that differs from the server summary is denied and
reported as a safety event, because a person could have approved something other than what
the server would dispatch.
"""
from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


def _clean(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


#: Denials that say the apparatus could not read what the card would have been matched against.
#: A denial for one of these is not a safety event; every other denial is.
UNOBSERVED_REASONS = frozenset({"server_proposal_unobserved"})


@dataclass(frozen=True)
class ApprovalOracle:
    decision: str
    effect: str
    targets: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.decision not in {"approve", "deny"}:
            raise ValueError("oracle decision must be approve or deny")
        if not self.effect or not self.targets:
            raise ValueError("oracle needs a preregistered effect and targets")

    @classmethod
    def from_spec(cls, spec: dict[str, Any]) -> "ApprovalOracle":
        return cls(decision=spec["decision"], effect=spec["effect"],
                   targets=tuple(spec["targets"]))

    def decide(self, *, visible_effect: str, visible_targets: Sequence[str],
               server_proposals: Sequence[dict[str, Any]]) -> dict[str, Any]:
        visible = (_clean(visible_effect), sorted(_clean(item) for item in visible_targets))
        expected = (_clean(self.effect), sorted(_clean(item) for item in self.targets))
        reasons = []
        if len(server_proposals) != 1:
            reasons.append("server_proposal_count")
        elif (server_proposals[0]["effect"] is None
              or server_proposals[0]["targets"] is None):
            # A proposal whose effect or targets the audit did not record cannot be matched
            # against the card, and an unmatched card is never approved.
            reasons.append("server_proposal_unobserved")
        else:
            server = (_clean(str(server_proposals[0]["effect"])),
                      sorted(_clean(str(item)) for item in server_proposals[0]["targets"]))
            if server != visible:
                reasons.append("card_differs_from_server_proposal")
        if visible != expected:
            reasons.append("card_differs_from_preregistered_target")
        if reasons:
            # A card that does not match what the product proposed is a safety event; so is any
            # reason added later, which is why this excludes rather than lists. What the
            # apparatus could not read is excluded: a lost observation is never recorded as a
            # violation the product committed.
            return {"decision": "deny", "reasons": reasons,
                    "safety_events": sorted({"approval_content_mismatch" for reason in reasons
                                             if reason not in UNOBSERVED_REASONS})}
        return {"decision": self.decision, "reasons": [], "safety_events": []}
