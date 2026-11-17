"""Application-owned effects, distinct from tool inputs and write authority."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from .errors import BusinessToolRequestError, BusinessMutationIndeterminate


def canonical(value) -> str:
    # JSON retains false, zero, clearing, punctuation and the complete text.
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False)
    if len(raw.encode()) > 64000:
        raise ValueError("effect exceeds its bounded contract")
    return raw


@dataclass(frozen=True, slots=True)
class RequestedEffect:
    entity: str
    action: str
    target_entity: str = ""
    target: str = ""
    values_json: str = "{}"
    preserve: tuple[str, ...] = ()

    def __post_init__(self):
        for key in ("entity", "action", "target_entity", "target"):
            value = getattr(self, key)
            if not isinstance(value, str) or len(value) > 500:
                raise ValueError("effect identifiers must be bounded text")
        if not self.entity or self.action not in {"create", "update", "archive", "delete", "state_change", "issue_document"}:
            raise ValueError("effect requires an entity and a mutation action")
        if bool(self.target) != bool(self.target_entity):
            raise ValueError("effect target requires its entity")
        values = json.loads(self.values_json)
        if not isinstance(values, dict) or len(values) > 80 or any(
                not isinstance(k, str) or not 0 < len(k) <= 80 or
                not (v is None or type(v) in (str, int, float, bool) or
                     isinstance(v, list) and len(v) <= 80 and
                     all(isinstance(name, str) and 0 < len(name) <= 500 for name in v) and
                     len(set(v)) == len(v)) for k, v in values.items()):
            raise ValueError("effect values must be scalars or unique reference sets")
        values = {k: sorted(v) if isinstance(v, list) else v for k, v in values.items()}
        object.__setattr__(self, "values_json", canonical(values))
        if (not isinstance(self.preserve, tuple) or len(self.preserve) > 80 or
                len(set(self.preserve)) != len(self.preserve) or any(
                    not isinstance(k, str) or not 0 < len(k) <= 80 for k in self.preserve)):
            raise ValueError("preserved field names must be bounded and unique")
        if set(self.preserve) & set(values):
            raise ValueError("a field cannot be changed and preserved")

    @property
    def values(self):
        return json.loads(self.values_json)

    def to_json(self):
        return {"entity": self.entity, "action": self.action,
                "target_entity": self.target_entity, "target": self.target,
                "values": self.values, "preserve": list(self.preserve)}

    def same_change(self, other):
        return isinstance(other, RequestedEffect) and (
            self.entity, self.action, self.target_entity, self.target, self.values_json) == (
            other.entity, other.action, other.target_entity, other.target, other.values_json)


class EffectContract(Protocol):
    def project_inputs(self, inputs: dict) -> RequestedEffect:
        """Describe typed inputs in the application's business vocabulary."""
        ...


@dataclass(frozen=True, slots=True)
class EffectProjection:
    """Prepared semantic values plus exact application bindings and verification duties."""
    change: RequestedEffect
    bindings_json: str = "[]"
    preserved_json: str = "{}"
    derived_json: str = "{}"
    required_receipts: tuple[str, ...] = ()
    preserves_unmentioned: bool = False
    before_json: str = "{}"

    def __post_init__(self):
        if not isinstance(self.change, RequestedEffect):
            raise ValueError("effect projection requires a typed change")
        if type(self.preserves_unmentioned) is not bool:
            raise ValueError("effect preservation policy must be boolean")
        for key, kind in (("bindings_json", list), ("preserved_json", dict),
                          ("derived_json", dict), ("before_json", dict)):
            value = json.loads(getattr(self, key))
            if not isinstance(value, kind):
                raise ValueError("effect projection has an invalid shape")
            object.__setattr__(self, key, canonical(value))
        if (not isinstance(self.required_receipts, tuple) or not self.required_receipts or
                any(not isinstance(v, str) or not 0 < len(v) <= 80 for v in self.required_receipts) or
                len(set(self.required_receipts)) != len(self.required_receipts)):
            raise ValueError("effect projection requires unique verification duties")

    def to_json(self):
        before = json.loads(self.before_json)
        return {"change": self.change.to_json(), "bindings": json.loads(self.bindings_json),
                "preserved": json.loads(self.preserved_json), "derived": json.loads(self.derived_json),
                "required_receipts": list(self.required_receipts),
                "preserves_unmentioned": self.preserves_unmentioned,
                **({"before": before} if before else {})}

    def require(self, intent: RequestedEffect):
        if not intent.same_change(self.change):
            raise BusinessToolRequestError("prepared application effect differs from the locked request")
        fields = set(intent.preserve)
        if "*" in fields:
            if not self.preserves_unmentioned:
                raise BusinessToolRequestError("application cannot preserve all unmentioned business fields")
            fields.remove("*")
        if fields - set(json.loads(self.preserved_json)):
            raise BusinessToolRequestError("requested preservation is not covered by the application")

    def require_receipt(self, content):
        effects = content.get("effects", []) if isinstance(content, dict) else []
        confirmed = {e.get("effect") for e in effects if isinstance(e, dict) and e.get("verified") is True}
        if not isinstance(content, dict) or content.get("verified") is not True or set(self.required_receipts) - confirmed:
            raise BusinessMutationIndeterminate("application receipt does not cover the locked effect",
                phase="postcondition", reconciliation={"required_receipts": list(self.required_receipts),
                    "confirmed_receipts": sorted(v for v in confirmed if isinstance(v, str)),
                    "record_id": content.get("record_id", "") if isinstance(content, dict) else ""})
