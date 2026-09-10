"""What the page says about the plan, one step at a time.

The plan is a small state machine rather than a page-wide landmark search. A step is `pending`,
`satisfied`, `not_applicable` or `refuted`, and only the current step is judged:

  satisfied       the page shows what this step predicted, or the URL is the request it named
  not_applicable  the page is a legitimate place to be on the way there, and says nothing
                  about this step -- no verdict, no conflict, the cursor does not move
  refuted         the page positively contradicts the step: its request was attempted and
                  refused, or the page reports an error where the step said it would succeed

Absence of evidence is `not_applicable`; refutation requires a positive contradiction. Once a
plan's write endpoint or two distinct steps are refuted, the plan is withdrawn and must no
longer be shown to the agent.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from .schemas import ActionReceipt, ContractStep, OperationContract, PageObservation

PENDING = "pending"
SATISFIED = "satisfied"
NOT_APPLICABLE = "not_applicable"
REFUTED = "refuted"

#: What the control plane observed in a trace window, without command attribution.
#: server-side; the browser never sees the control plane.
WRITE_NONE = "none"          # no write decision observed in this window
WRITE_PERMITTED = "permitted"   # permission consumed; forwarding/save not established
WRITE_REFUSED = "refused"       # refused before reaching the application: certainly not written
WRITE_MIXED = "mixed"           # one window allowed one request and refused another:
                                # part of the change may have been made, part certainly not
WRITE_UNKNOWN = "unknown"       # the control plane could not be read: never assume either way

#: An action that changes nothing cannot prove a step wrong. Repeated observations of one
#: refusal page are still one piece of evidence and cannot refute unattempted steps.
_ATTEMPTS_NOTHING = frozenset({"observe"})

RouteIdentity = Callable[[str], tuple[str, str]]


def opaque_route_identity(_url: str) -> tuple[str, str]:
    """Default for applications that did not declare source-contract route semantics."""
    return "", ""


#: Text an application shows when it has refused to do something. Matching these is what turns
#: "the page is not what I expected" into "the page says no".
_REFUSAL_MARKS = re.compile(
    r"action refused|not authori[sz]ed|permission denied|access denied|"
    r"you do not have permission|error occurred|following errors", re.I)


@dataclass
class StepState:
    step: ContractStep
    status: str = PENDING
    why: str = ""


@dataclass
class PlanState:
    """The contract plus what the run has learned about it."""
    contract: OperationContract
    steps: list[StepState] = field(default_factory=list)
    #: Alternative ways in. Reaching any one of them is entry; the others are then simply
    #: unused, which is not evidence against them.
    candidates: list[StepState] = field(default_factory=list)
    withdrawn: bool = False
    withdrawn_because: str = ""
    #: A write the plan led to was authorised and its result is not yet confirmed. The plan
    #: stops prompting, but nothing about it is declared finished.
    paused: bool = False
    paused_because: str = ""
    #: Durable notes. The caller must keep these out of reach of context trimming: a
    #: disconfirmation the agent forgets is a disconfirmation that never happened.
    standing_notes: list[str] = field(default_factory=list)
    #: Refused requests whose identity does not match the plan. Not evidence against it.
    unattributed_refusals: int = 0
    route_identity: RouteIdentity = field(default=opaque_route_identity, repr=False)

    @staticmethod
    def of(contract: OperationContract,
           route_identity: RouteIdentity = opaque_route_identity) -> "PlanState":
        """Entry candidates are alternatives; the steps are a sequence.

        Reaching any candidate satisfies the entry requirement and leaves the unused
        alternatives `not_applicable`; candidate order must not constrain the route taken.
        """
        if not callable(route_identity):
            raise TypeError("route_identity must be callable")
        return PlanState(contract=contract, route_identity=route_identity,
                         candidates=[StepState(c) for c in contract.entry_candidates],
                         steps=[StepState(s) for s in contract.steps])

    @property
    def entry_reached(self) -> bool:
        """No candidates means nothing to enter by; otherwise one of them must have matched."""
        return not self.candidates or any(c.status == SATISFIED for c in self.candidates)

    @property
    def current(self) -> StepState | None:
        """What is being judged now: an entry, if one is still needed, else the next step."""
        if not self.entry_reached:
            return next((c for c in self.candidates if c.status == PENDING), None)
        return next((s for s in self.steps if s.status == PENDING), None)

    @property
    def all_states(self) -> list[StepState]:
        return list(self.candidates) + list(self.steps)

    @property
    def refuted_steps(self) -> list[StepState]:
        return [s for s in self.all_states if s.status == REFUTED]

    def counts(self) -> dict:
        out = {PENDING: 0, SATISFIED: 0, NOT_APPLICABLE: 0, REFUTED: 0}
        for s in self.all_states:
            out[s.status] += 1
        return out

    # -- the judgement ------------------------------------------------------------------
    def observe(self, obs: PageObservation, receipt: ActionReceipt,
                write_outcome: str = WRITE_NONE, *, refused_requests: list[str] | None = None) -> dict:
        """Judge the current step against this page. Returns an event for the ledger."""
        step = self.current
        if self.withdrawn:
            return {"verdict": "no_active_step", "withdrawn": True}
        if self.paused:
            return {"verdict": "awaiting_confirmation", "paused": True}
        page = " ".join([obs.title, *obs.landmarks, *obs.messages]).lower()
        refusal_on_page = bool(_REFUSAL_MARKS.search(page))
        attempted = receipt.action not in _ATTEMPTS_NOTHING
        here = self.route_identity(obs.url or "")
        if write_outcome == WRITE_REFUSED:
            # Only enforcement decisions identify a refused request. The current/previous
            # page and action arguments do not: AJAX may submit elsewhere. Identity includes
            # the webapp; a beta/edit refusal says nothing about alpha/edit.
            refused = {tuple(r.lower().split("/", 1)) for r in (refused_requests or [])}
            mutation = self.contract.mutation
            if mutation and _identity(mutation) in refused:
                return self.refute_mutation("the enforcement point refused that request")
            for candidate in self.all_states:
                if candidate.status == PENDING and _identity(candidate.step) in refused:
                    return self._settle(candidate, REFUTED,
                                        "the enforcement point refused the write this step named "
                                        f"({candidate.step.webapp}/{candidate.step.request})")
            return self._unattributed_refusal()

        if step is None:
            return {"verdict": "no_active_step", "withdrawn": False}
        on_named_route = bool(step.step.request) and here == _identity(step.step)

        # Reaching a named route is not enough when that page refuses the operation. A
        # positive refusal on the step's own route refutes the step before route satisfaction
        # is considered.
        if on_named_route and refusal_on_page and receipt.ok and attempted:
            return self._settle(step, REFUTED,
                                f"the page at {step.step.webapp}/{step.step.request} reports a "
                                f"refusal or error, so arriving there is not enough")

        # An entry is whichever candidate was actually reached. The others are then unused,
        # which says nothing against them.
        if not self.entry_reached:
            reached = self._candidate_at(here, page)
            if reached is not None:
                if refusal_on_page:
                    if receipt.ok and attempted:
                        return self._settle(reached, REFUTED,
                                            "the page at this candidate reports a refusal or error")
                    return {"verdict": NOT_APPLICABLE, "step": reached.step.intent}
                for other in self.candidates:
                    if other is not reached and other.status == PENDING:
                        other.status = NOT_APPLICABLE
                        other.why = "another entry was used; this route was simply not taken"
                return self._settle(reached, SATISFIED,
                                    "this is the entry the run actually reached")

        # Arriving at the request the step named is the strongest confirmation there is; the
        # landmark is only a proxy for it.
        if on_named_route and not refusal_on_page:
            return self._settle(step, SATISFIED, "the page is the request this step named")
        landmark = (step.step.landmark or "").strip().lower()
        if not step.step.request and landmark and landmark in page and not refusal_on_page:
            return self._settle(step, SATISFIED, f"the page shows {step.step.landmark!r}")

        # The page says no, where the step said it would work -- but only if this action tried
        # anything. Re-reading a page is not a second attempt and not a second contradiction.
        if not step.step.request and landmark and landmark in page \
                and refusal_on_page and receipt.ok and attempted:
            return self._settle(step, REFUTED,
                                "the page reports a refusal or error where this step expected "
                                "to succeed")

        # Everything else is absence of evidence. Being somewhere else on the way is normal.
        return {"verdict": NOT_APPLICABLE, "step": step.step.intent,
                "why": "this page neither shows nor contradicts the current step"}

    def _candidate_at(self, here: tuple, page_text: str) -> "StepState | None":
        """Whichever pending candidate this page is, by route or by its landmark."""
        for c in self.candidates:
            if c.status != PENDING:
                continue
            named = (c.step.request or "").lower()
            if named and here == ((c.step.webapp or "").lower(), named):
                return c
            landmark = (c.step.landmark or "").strip().lower()
            if not named and landmark and landmark in page_text:
                return c
        return None

    def _unattributed_refusal(self) -> dict:
        self.unattributed_refusals += 1
        note = ("An enforcement decision refused a request. Its identity does not establish "
                "which step of the plan, if any, was attempted. The plan remains unrefuted.")
        self.standing_notes.append(note)
        event = {"verdict": NOT_APPLICABLE, "step": "(unattributed)", "why": "a write was refused and "
                 "could not be tied to a step of the plan", "standing_note": note}
        return event

    def skip_current(self, why: str) -> dict:
        step = self.current
        if step is None:
            return {"verdict": "no_active_step"}
        return self._settle(step, NOT_APPLICABLE, why)

    def _settle(self, step: StepState, status: str, why: str) -> dict:
        step.status, step.why = status, why
        event = {"verdict": status, "step": step.step.intent, "why": why}
        if status == REFUTED:
            note = f"The plan said: {step.step.intent}. That turned out to be wrong -- {why}."
            self.standing_notes.append(note)
            event["standing_note"] = note
            self._consider_withdrawal(step)
        return event

    def _consider_withdrawal(self, step: StepState | None) -> None:
        """Withdraw the plan when it can no longer be trusted, not on a counter.

        Two triggers, both about substance. Its write endpoint being refuted means the one
        thing the contract was for is wrong. Two distinct steps refuted means the route, not a
        detail, is wrong. A single wrong intermediate step is survivable and does not withdraw
        a plan that may still be right about where the work happens.
        """
        mutation = self.contract.mutation
        if step is not None and mutation and step.step.request and \
                step.step.request == mutation.request and step.step.webapp == mutation.webapp:
            self._withdraw("the write endpoint it predicted was refused")
            return
        # A refused write and a page that merely fails to show a landmark are different kinds
        # of evidence. An enforcement refusal withdraws the plan immediately; repeated copies
        # of that decision add no evidence. Page-text refutations remain inferences and need
        # two distinct contradicted steps.
        if any("refused the write" in s.why for s in self.refuted_steps):
            self._withdraw("a write it led to was refused by the application")
        elif len(self.refuted_steps) >= 2:
            self._withdraw(f"{len(self.refuted_steps)} of its steps were contradicted by the "
                           f"application")

    def _withdraw(self, why: str) -> None:
        if self.withdrawn:
            return
        self.withdrawn = True
        self.withdrawn_because = why
        self.standing_notes.append(
            f"The plan has been withdrawn: {why}. Ignore it entirely and "
            f"work from what the pages actually offer.")

    def pause_for_confirmation(self, why: str) -> None:
        """Pause a plan after its write is authorised while the result is unconfirmed.

        The enforcement point decides before the proxy forwards, so a consumed permit does
        not establish forwarding, saving or task completion. Pausing prevents the plan from
        prompting another submission while the caller verifies the actual application state.
        """
        if self.withdrawn or self.paused:
            return
        self.paused = True
        self.paused_because = why
        self.standing_notes.append(
            "Write permission was consumed; forwarding and saving are not established. "
            "The plan is paused so it does not prompt another submission. "
            "Check the page for the change rather than submitting again.")

    def refute_mutation(self, why: str) -> dict:
        """The predicted write endpoint was refused, whatever step we were on."""
        mutation = self.contract.mutation
        if mutation is None:
            return {"verdict": "no_mutation"}
        for s in self.steps:
            if s.step.request == mutation.request and s.step.webapp == mutation.webapp \
                    and s.status == PENDING:
                return self._settle(s, REFUTED, why)
        note = (f"The plan predicted the change would be submitted by "
                f"{mutation.webapp}/{mutation.request}. That was wrong -- {why}.")
        self.standing_notes.append(note)
        self._withdraw("the write endpoint it predicted was refused")
        return {"verdict": REFUTED, "step": "mutation", "why": why, "standing_note": note}


def _identity(step: ContractStep) -> tuple[str, str]:
    return ((step.webapp or "").lower(), (step.request or "").lower())
