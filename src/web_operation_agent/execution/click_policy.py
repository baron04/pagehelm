"""An application's own answer to "may the assistant click this, here?".

Generic click is a potentially writing browser action, so a deployment disables it unless
the application says otherwise. Some applications have pages whose controls a reviewer
has checked -- view switches, pagers, tabs, links to other reviewed pages -- and want
those clickable while everything else stays refused. A `ClickPolicy` is how an adapter
says so: it sees the current observation and the one element the model named, and
answers with an empty string to allow the click or a short reason to refuse it.

The policy only ever narrows a click that is otherwise disabled. It does not grant a
write, skip navigation confirmation, or change how a receipt is settled; a refused click
is an ordinary pre-dispatch policy block and reaches no browser.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..schemas import ElementRef, PageObservation


@runtime_checkable
class ClickPolicy(Protocol):
    def decide(self, observation: PageObservation, element: ElementRef) -> str:
        """"" to allow this click on this page, or a short reason to refuse it."""
        ...


def click_policy_refusal(policy: ClickPolicy, observation: PageObservation,
                         ref: object) -> str:
    """The policy's reason for refusing a click on `ref`, or "" when it allows it.

    An element that is not in the observation is refused here rather than left for the
    browser's stale-reference check: the policy has nothing to judge.
    """
    element = next((item for item in observation.elements if item.ref == ref), None)
    if element is None:
        return "that element is not on the current page"
    try:
        reason = policy.decide(observation, element)
    except Exception as exc:  # noqa: BLE001 -- a broken policy fails closed
        return f"the application's click policy failed ({type(exc).__name__})"
    return reason if isinstance(reason, str) else "the click policy gave no answer"
