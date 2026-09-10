"""Application-neutral browser execution contract used by the server Agent.

The shipping browser implementation is ``remote.RemoteBrowserClient``. It sends the one
versioned ``schemas.ActionCommand`` wire shape to the embedded runtime and receives a
versioned ``ActionReceipt``. Keeping this module limited to the structural server contract
and typed transport outcomes prevents a second browser engine from silently diverging.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from .schemas import ActionReceipt, PageObservation


class TransportError(Exception):
    """The browser channel failed before a definite action receipt was available."""


class TurnCancelled(TransportError):
    """The authenticated user cancelled before the browser claimed the next action."""


@runtime_checkable
class BrowserExecutor(Protocol):
    """Small synchronous surface consumed by ``ServerAgentSession``."""

    def observe(self) -> PageObservation: ...

    def execute(self, action: str, args: dict | None = None, *,
                expect_navigation: bool = False) -> ActionReceipt: ...

    def console(self) -> list[str]: ...
