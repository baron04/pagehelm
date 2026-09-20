# --------------------------------------------------------------------------------------
# Control plane (server-side only; never serialised towards the browser or the model)
# --------------------------------------------------------------------------------------
from dataclasses import dataclass, field

from .common import digest

@dataclass(frozen=True)
class ControlTicket:
    webapp: str
    request: str
    targets: tuple[dict, ...]
    changes: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {"webapp": self.webapp, "request": self.request,
                "targets": [dict(t) for t in self.targets], "changes": dict(self.changes)}


@dataclass(frozen=True)
class ControlPlan:
    """What the user authorised for this run, derived from the public task spec.

    Held by the session, installed into the enforcing gateway, and never placed in a model
    prompt, a browser message or a contract. `redacted()` is what may appear in a ledger.
    """
    plan_id: str
    task_ref: str
    tickets: tuple[ControlTicket, ...]

    def to_json(self) -> dict:
        return {"plan_id": self.plan_id, "task_ref": self.task_ref,
                "tickets": [t.to_json() for t in self.tickets]}

    @property
    def content_digest(self) -> str:
        return digest(self.to_json())

    def redacted(self) -> dict:
        """Ledger-safe: proves a plan existed and was stable, reveals nothing about its content."""
        return {"plan_id": self.plan_id, "task_ref": self.task_ref,
                "ticket_count": len(self.tickets), "digest": self.content_digest}
