"""Control-plane witnessing and conservative settlement of browser writes.

The browser receipt and the control gateway answer different questions: the receipt says
what happened to a command, while the gateway says whether a request consumed a permit or
was refused.  ``WriteWatch`` owns the watermark and the unresolved state needed to combine
those observations without ever attributing a late decision to the next command.
"""
from __future__ import annotations

from .controlplane import ControlPlaneClient, ControlPlaneError
from ..execution.grounding import WRITE_MIXED, WRITE_UNKNOWN
from ..state.ledger import SessionLedger
from ..schemas import ActionReceipt


# Some applications write during navigation or onchange.  Only observation and waiting are
# intrinsically non-writing browser actions.
WRITE_CAPABLE = frozenset({"click", "submit", "fill", "select", "goto", "back"})

# Worker failures that prove the action did not reach the page.  In particular,
# ``navigation_timeout`` stays out: the click may already have fired a request while the
# runtime was waiting for the resulting navigation.
PRE_DISPATCH_ERRORS = frozenset({
    "stale_ref", "unknown_ref", "bad_arguments", "no_such_action",
    "deadline_expired", "stale_observation", "capability_refused", "runtime_replaced",
})


class WatchState:
    """Expose selected ``WriteWatch`` state on its owning session.

    A few tests and the result projector intentionally treat these three values as session
    attributes.  The descriptor preserves that contract without keeping a second copy.  Its
    fallback also supports the small ``object.__new__`` policy test that never constructs a
    complete session.
    """

    def __init__(self, name: str, default):
        self.name = name
        self.default = default
        self.fallback = f"_write_watch_{name}"

    def __get__(self, instance, owner=None):
        if instance is None:
            return self
        watcher = instance.__dict__.get("_writes")
        return (getattr(watcher, self.name) if watcher is not None
                else instance.__dict__.get(self.fallback, self.default))

    def __set__(self, instance, value) -> None:
        watcher = instance.__dict__.get("_writes")
        if watcher is None:
            instance.__dict__[self.fallback] = value
        else:
            setattr(watcher, self.name, value)


class WriteWatch:
    """Witness one run's control decisions and settle potentially writing actions."""

    def __init__(self, *, control: ControlPlaneClient, ledger: SessionLedger, run_id: str):
        self.control = control
        self.ledger = ledger
        self.run_id = run_id
        self.writes_permitted = 0
        self.write_refusals = 0
        self.pending_write = False
        self.uncertain_write = ""
        self.awaiting_control = False
        self.control_seen = 0
        self.control_started = False

    def begin_control(self) -> None:
        if self.control_started:
            return
        self.control.begin_run(self.run_id)
        self.control_started = True
        self.ledger.append("control_run_started")

    def end_control(self) -> ControlPlaneError | None:
        if not self.control_started:
            return None
        end_run = getattr(self.control, "end_run", None)
        if not callable(end_run):
            self.control_started = False
            return None
        try:
            end_run(self.run_id)
            self.control_started = False
            self.ledger.append("control_run_released")
            return None
        except Exception as exc:
            self.ledger.append(
                "control_run_release_failed", error_type=type(exc).__name__)
            return (exc if isinstance(exc, ControlPlaneError)
                    else ControlPlaneError("control run release failed"))

    def start_observing(self) -> None:
        """Take the initial trace watermark after the run lease is established."""
        _, self.control_seen = self.control.since(0)

    def expect_decision(self, action_may_write: bool) -> None:
        """Remember whether the command about to be dispatched may produce a write."""
        self.awaiting_control = action_may_write

    def write_outcome(self, receipt: ActionReceipt, step: int,
                      *, action_may_write: bool | None = None) -> dict:
        """Read decisions after a receipt without assigning them to that command.

        An empty trace does not prove a failed click never dispatched. A permit may arrive
        later, so ambiguous dispatch failures stop immediately, even with no event yet.
        """
        if action_may_write is None:
            action_may_write = receipt.action in WRITE_CAPABLE
        try:
            events, self.control_seen = self.control.since(self.control_seen)
        except ControlPlaneError as exc:
            state = (WRITE_UNKNOWN
                     if self.pending_write or self.awaiting_control else "control_error")
            return {"state": state,
                    "detail": f"control plane unreadable: {exc}"[:300],
                    "permitted": 0, "refused": 0, "refused_requests": []}
        self.awaiting_control = False
        out = ControlPlaneClient.outcome_of(events)
        was_pending = self.pending_write
        self.writes_permitted += out["permitted"]
        self.write_refusals += out["refused"]
        self.pending_write |= bool(out["permitted"])
        detail = ""
        if out["state"] == WRITE_MIXED or (was_pending and out["refused"]):
            detail = ("authorised and refused requests were observed before the pending write "
                      "was confirmed; a partial change cannot be excluded")
        elif out["permitted"] and not action_may_write:
            detail = "late write permission observed after a non-dispatching action; cause unknown"
        elif not receipt.ok and (self.pending_write or (
                action_may_write and receipt.error_code not in PRE_DISPATCH_ERRORS)):
            detail = ("a potentially writing action lost its result; an empty decision window "
                      "does not prove non-dispatch and retry is unsafe")
        self.ledger.append("write_outcome", step=step,
                           observed_after_command_id=receipt.command_id,
                           attribution="unattributed", **out, detail=detail)
        if detail:
            return {**out, "state": WRITE_UNKNOWN, "detail": detail}
        if out["refused"]:
            self.ledger.append("write_refused", step=step, attribution="unattributed",
                               refusals=out["refused"])
        if out["permitted"]:
            self.ledger.append("write_permitted", step=step, attribution="unattributed")
        return {**out, "detail": ""}

    def refresh_control(self) -> dict:
        """Reconcile cumulative decisions before dispatch and on every terminal path."""
        out = self.control.outcome()
        permits = int(out.get("permit_consumed") or 0)
        refusals = int(out.get("write_blocked") or 0)
        seen = int(out.get("trace_events") or 0)
        if (permits < self.writes_permitted or refusals < self.write_refusals
                or seen < self.control_seen):
            raise ControlPlaneError("control trace rewound within the run")
        new_permits = permits - self.writes_permitted
        new_refusals = refusals - self.write_refusals
        if new_permits or (self.pending_write and new_refusals):
            self.uncertain_write = (
                "late authorised/refused decisions arrived without a correlated write result; "
                "a partial or unknown change cannot be excluded")
        if new_permits or new_refusals:
            self.ledger.append("write_reconciled", attribution="unattributed",
                               new_permits=new_permits, new_refusals=new_refusals,
                               consumed_requests=out.get("consumed_requests", []),
                               blocked_requests=out.get("blocked_requests", []))
        self.pending_write |= bool(permits)
        self.writes_permitted = permits
        self.write_refusals = refusals
        self.control_seen = seen
        return out

    def settle_write(self, write: dict) -> tuple[str, str]:
        """Return the terminal state for one observed write outcome, if there is one."""
        state = write["state"]
        if state == WRITE_UNKNOWN:
            self.uncertain_write = write["detail"]
            return "indeterminate_write", write["detail"]
        if state == "control_error":
            return "control_plane_error", write["detail"]
        return "", ""

    def mark_control_unavailable(self) -> None:
        if self.pending_write or self.awaiting_control:
            self.uncertain_write = (
                "control evidence unavailable while a write is unresolved")
