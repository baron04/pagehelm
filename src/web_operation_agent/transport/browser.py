"""Application-neutral browser execution contract used by the server Agent.

The shipping browser implementation is ``remote.RemoteBrowserClient``. It sends the one
versioned ``schemas.ActionCommand`` wire shape to the embedded runtime and receives a
versioned ``ActionReceipt``. Keeping this module limited to the structural server contract
and typed transport outcomes prevents a second browser engine from silently diverging.
"""
from __future__ import annotations

import uuid
import math

from typing import Protocol, runtime_checkable

from ..schemas import (SCHEMA_VERSIONS, ActionCommand, ActionEnvelope, ActionReceipt,
                      ObservationEnvelope, PageObservation, RECEIPT_ERRORS)
from .protocol import RemoteProtocolError, _copy_wire, _now_ms, _remote_args_echo
from ..execution.events import observed_interval


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


class DeadlineBrowserExecutor(BrowserExecutor, Protocol):
    """Server transport that also bounds an action by the remaining turn time."""

    def execute_with_budget(self, action: str, args: dict | None = None, *,
                            timeout_s: float,
                            expect_navigation: bool = False) -> ActionReceipt: ...


class RemoteBrowserClient:
    """Synchronous browser interface consumed unchanged by ``ServerAgentSession``."""

    def __init__(self, channel: RemoteSessionChannel, *, timeout_s: float = 90,
                 turn_id: str = ""):
        self.channel = channel
        self.timeout_s = timeout_s
        self.turn_id = turn_id
        self.command_count = 0
        self._delivered_observation: ObservationEnvelope | None = None

    def _deliver(self, envelope: ObservationEnvelope) -> PageObservation:
        self._delivered_observation = _copy_wire(envelope)
        return _copy_wire(envelope).observation

    def _fresh_fact_receipt(self, *, action_id: str, action: str, args: dict,
                            before: ObservationEnvelope,
                            fresh: ObservationEnvelope) -> ActionReceipt:
        """Report a page change detected before dispatch, without inventing execution."""
        self._delivered_observation = _copy_wire(fresh)
        observe_only = action == "observe"
        return ActionReceipt(
            schema_version=SCHEMA_VERSIONS["ActionReceipt"],
            command_id=action_id,
            action=action,
            args_echo=_remote_args_echo(args),
            ok=observe_only,
            error_code="" if observe_only else "stale_observation",
            error_detail=("" if observe_only else
                          "the page changed before this action was dispatched"),
            url_before=before.observation.url,
            url_after=fresh.observation.url,
            navigated=before.observation.url != fresh.observation.url,
            epoch_before=before.page_epoch,
            epoch_after=fresh.page_epoch,
            observation=_copy_wire(fresh).observation,
        )

    @observed_interval("page")
    def start(self, **_ignored) -> dict:
        try:
            hello = self.channel.wait_for_client(self.timeout_s)
            observation = self.channel.wait_for_observation(timeout_s=self.timeout_s)
        except (TimeoutError, RemoteProtocolError) as exc:
            raise TransportError(str(exc)) from exc
        self._deliver(observation)
        return {"ok": True, "client_id": hello.client_id,
                "url": observation.observation.url, "embedded": True}

    @observed_interval("page")
    def observe(self) -> PageObservation:
        try:
            observation = self.channel.wait_for_observation(timeout_s=self.timeout_s)
            return self._deliver(observation)
        except (TimeoutError, RemoteProtocolError) as exc:
            raise TransportError(str(exc)) from exc

    def execute(self, action: str, args: dict | None = None, *,
                expect_navigation: bool = False) -> ActionReceipt:
        return self._execute(action, args, expect_navigation=expect_navigation,
                             timeout_s=self.timeout_s)

    def execute_with_budget(self, action: str, args: dict | None = None, *,
                            timeout_s: float,
                            expect_navigation: bool = False) -> ActionReceipt:
        if (isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float))
                or not math.isfinite(timeout_s) or timeout_s <= 0):
            raise ValueError("action time budget must be positive and finite")
        return self._execute(action, args, expect_navigation=expect_navigation,
                             timeout_s=min(self.timeout_s, timeout_s))

    @observed_interval("page")
    def _execute(self, action: str, args: dict | None, *,
                 timeout_s: float, expect_navigation: bool) -> ActionReceipt:
        observation = self._delivered_observation
        if observation is None:
            raise TransportError("no page observation has been delivered to the server Agent")
        self.command_count += 1
        action_id = f"a-{uuid.uuid4().hex}"
        turn_id = self.turn_id or f"t-{self.command_count:06d}"
        command = ActionCommand(command_id=action_id, action=action, args=args or {},
                                expect_navigation=expect_navigation)
        envelope = ActionEnvelope(
            schema_version=SCHEMA_VERSIONS["ActionEnvelope"],
            session_id=self.channel.session_id,
            turn_id=turn_id,
            action_id=action_id,
            based_on_observation_id=observation.observation_id,
            page_epoch=observation.page_epoch,
            deadline=_now_ms() + int(timeout_s * 1000),
            command_digest=command.content_digest,
            command=command,
        )
        latest = self.channel.latest_observation_envelope
        if (latest is not None
                and (latest.page_epoch != observation.page_epoch
                     or latest.observation_id != observation.observation_id)):
            return self._fresh_fact_receipt(
                action_id=action_id, action=action, args=args or {},
                before=observation, fresh=latest)
        try:
            self.channel.enqueue_action(envelope)
            result = self.channel.wait_for_result(action_id, timeout_s=timeout_s)
        except (TimeoutError, RemoteProtocolError) as exc:
            if isinstance(exc, RemoteProtocolError) and exc.code == "turn_cancelled":
                raise TurnCancelled(str(exc)) from exc
            if isinstance(exc, RemoteProtocolError) and exc.code == "stale_observation":
                fresh = self.channel.latest_observation_envelope
                if fresh is not None:
                    return self._fresh_fact_receipt(
                        action_id=action_id, action=action, args=args or {},
                        before=observation, fresh=fresh)
            raise TransportError(str(exc)) from exc
        if result.observation_after is not None:
            self._deliver(result.observation_after)
        if result.status == "indeterminate":
            raise TransportError(result.error_detail or "remote action result is indeterminate")
        if result.status == "completed":
            assert result.receipt is not None
            return result.receipt
        assert result.observation_after is not None
        fresh = result.observation_after.observation
        return ActionReceipt(
            schema_version=SCHEMA_VERSIONS["ActionReceipt"],
            command_id=action_id,
            action=action,
            args_echo=_remote_args_echo(args or {}),
            ok=False,
            error_code=(result.error_code
                        if result.error_code in RECEIPT_ERRORS else "page_error"),
            error_detail=result.error_detail,
            url_before=observation.observation.url,
            url_after=fresh.url,
            navigated=observation.observation.url != fresh.url,
            epoch_before=observation.page_epoch,
            epoch_after=result.observation_after.page_epoch,
            observation=fresh,
        )

    def console(self) -> list[str]:
        return []

    def close(self) -> None:
        self.channel.close()
