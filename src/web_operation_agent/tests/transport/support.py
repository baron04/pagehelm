"""Shared transport test builders; this module contains no test cases."""
from __future__ import annotations

import time

from web_operation_agent.state.identity import PrincipalContext
from web_operation_agent.schemas import (
    SCHEMA_VERSIONS, ActionCommand, ActionEnvelope, ActionReceipt,
    ActionResultEnvelope, ClientHello, ElementRef, ObservationEnvelope,
    PageObservation,
)


ORIGIN = "http://app.test"
DEFAULT_LEASE = object()


def hello(session_id: str, *, cursor: str = "",
          runtime_instance_id: str = "runtime-document-1",
          client_id: str = "browser-tab-1",
          application_id: str = "", principal_digest: str = "") -> ClientHello:
    return ClientHello(
        schema_version=SCHEMA_VERSIONS["ClientHello"], session_id=session_id,
        client_id=client_id, runtime="test-runtime/1",
        runtime_instance_id=runtime_instance_id,
        capabilities=("dom.observe", "dom.click", "dom.fill", "dom.select", "dom.submit",
                      "page.goto", "page.back", "page.scroll", "page.wait"), page_origin=ORIGIN,
        application_id=application_id, principal_digest=principal_digest,
        last_event_id=cursor,
    )


def principal(now_ms: int, *, actor_id: str = "actor-1") -> PrincipalContext:
    return PrincipalContext(
        1, "sample", "tenant-1", "workspace-1", actor_id,
        "delegation-1", now_ms, now_ms + 60_000,
        ("companies:read", "page:operate"),
    )


def observation(session_id: str, epoch: int, *, caused_by: str = "",
                url: str = ORIGIN + "/start") -> ObservationEnvelope:
    page = PageObservation(
        schema_version=SCHEMA_VERSIONS["PageObservation"], epoch=epoch,
        url=url, title=f"Page {epoch}", landmarks=["Fixture"],
        elements=[ElementRef(ref="e0", role="link", name="Next", tag="a",
                             epoch=epoch, attrs={"href": "/next"})],
        content=f"[0]<a href=/next>Next />\npage {epoch}",
    )
    return ObservationEnvelope(
        schema_version=SCHEMA_VERSIONS["ObservationEnvelope"],
        session_id=session_id, observation_id=f"o-{epoch}", page_epoch=epoch,
        observation=page, caused_by_action_id=caused_by,
    )


def action_for(session_id: str, obs: ObservationEnvelope, *, action_id: str = "a-1",
               deadline: int | None = None) -> ActionEnvelope:
    command = ActionCommand(command_id=action_id, action="click", args={"ref": "e0"},
                            expect_navigation=True)
    return ActionEnvelope(
        schema_version=SCHEMA_VERSIONS["ActionEnvelope"], session_id=session_id,
        turn_id="t-1", action_id=action_id,
        based_on_observation_id=obs.observation_id, page_epoch=obs.page_epoch,
        deadline=deadline or int(time.time() * 1000) + 5000,
        command_digest=command.content_digest, command=command,
    )


def completed(action: ActionEnvelope, after: ObservationEnvelope, *, elapsed_ms: int = 7,
              url_before: str = ORIGIN + "/start"
              ) -> ActionResultEnvelope:
    receipt = ActionReceipt(
        schema_version=SCHEMA_VERSIONS["ActionReceipt"], command_id=action.action_id,
        action=action.command.action, args_echo={"ref": "e0"}, ok=True,
        url_before=url_before, url_after=after.observation.url,
        navigated=True, epoch_before=action.page_epoch, epoch_after=after.page_epoch,
        elapsed_ms=elapsed_ms, observation=after.observation,
    )
    return ActionResultEnvelope(
        schema_version=SCHEMA_VERSIONS["ActionResultEnvelope"],
        session_id=action.session_id, turn_id=action.turn_id,
        action_id=action.action_id,
        based_on_observation_id=action.based_on_observation_id,
        page_epoch=action.page_epoch, command_digest=action.command_digest,
        status="completed",
        observation_after=after, receipt=receipt,
    )
