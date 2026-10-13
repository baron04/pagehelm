"""Full user-reviewable effects, separate from private native request bodies."""
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ApprovalDetail:
    kind: str
    label: str
    text: str

    def __post_init__(self):
        if self.kind not in {"change", "preserved", "derived", "side_effect", "target_rule", "body"}:
            raise ValueError("unsupported approval detail kind")
        if not isinstance(self.label, str) or not 1 <= len(self.label) <= 200:
            raise ValueError("approval detail requires a bounded label")
        if not isinstance(self.text, str) or len(self.text) > 32000:
            raise ValueError("approval detail exceeds its text limit")

    def to_json(self):
        return {"kind": self.kind, "label": self.label, "text": self.text}


def validate_details(details):
    if (not isinstance(details, tuple) or len(details) > 200
            or any(not isinstance(d, ApprovalDetail) for d in details)
            or sum(len(d.label.encode()) + len(d.text.encode()) for d in details) > 64000):
        raise ValueError("approval details exceed their bounded projection")
