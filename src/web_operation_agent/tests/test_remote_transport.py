from __future__ import annotations

import dataclasses
import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from web_operation_agent.transport import (BrowserExecutor, TransportError, TurnCancelled)
from web_operation_agent.transport.gateway import EmbeddedGateway
from web_operation_agent.identity import OperationContext, PrincipalContext
from web_operation_agent.transport import (RemoteBrowserClient, RemoteProtocolError,
                                         RemoteSessionClosed, RemoteSessionLimits,
                                         RemoteSessionRegistry)
from web_operation_agent.schemas import (
    SCHEMA_VERSIONS,
    ActionCommand,
    ActionEnvelope,
    ActionReceipt,
    ActionResultEnvelope,
    ApprovalDecision,
    ApprovalRequest,
    CancellationRequest,
    ClientHello,
    ElementRef,
    ObservationEnvelope,
    PageObservation,
)


ORIGIN = "http://app.test"
DEFAULT_LEASE = object()


def hello(session_id: str, *, cursor: str = "",
          runtime_instance_id: str = "runtime-document-1",
          application_id: str = "", principal_digest: str = "") -> ClientHello:
    return ClientHello(
        schema_version=SCHEMA_VERSIONS["ClientHello"], session_id=session_id,
        client_id="browser-tab-1", runtime="test-runtime/1",
        runtime_instance_id=runtime_instance_id,
        capabilities=("dom.observe", "dom.click", "dom.fill", "dom.select", "dom.submit",
                      "page.goto", "page.back", "page.wait"), page_origin=ORIGIN,
        application_id=application_id, principal_digest=principal_digest,
        last_event_id=cursor,
    )


def principal(now_ms: int, *, actor_id: str = "actor-1") -> PrincipalContext:
    return PrincipalContext(
        1, "twenty", "tenant-1", "workspace-1", actor_id,
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


class ChannelTests(unittest.TestCase):
    def setUp(self):
        registry = RemoteSessionRegistry()
        self.grant, self.channel = registry.create(allowed_origins=[ORIGIN],
                                                   session_id="test-session")
        reply = self.channel.accept_hello(hello(self.grant.session_id))
        self.lease = reply["lease_id"]
        self.first = observation(self.grant.session_id, 1)
        self.channel.accept_observation(self.first, lease_id=self.lease)

    def await_action_event(self, cursor: str = "e00000001", *, timeout_s: float = 5.0):
        """Wait for the action a worker thread is about to enqueue.

        `events_after` returns as soon as *anything* follows the cursor, and a turn emits
        `message_accepted` before the runtime is ever asked to act. Taking the first batch
        therefore raced the worker thread and lost often enough to make the suite flaky.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            events = self.channel.events_after(cursor, lease_id=self.lease, wait_s=0.2)
            for event in events:
                if event.kind == "action":
                    return event
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"no action event after {cursor}; saw {[e.kind for e in events]}")

    def test_event_replay_is_same_action_and_result_is_idempotent(self):
        action = action_for(self.grant.session_id, self.first)
        event = self.channel.enqueue_action(action)
        replay = self.channel.events_after("e00000001", lease_id=self.lease)
        self.assertEqual([e.to_json() for e in replay], [event.to_json()])
        self.assertEqual(replay[0].action().idempotency_key, action.idempotency_key)

        after = observation(self.grant.session_id, 2, caused_by=action.action_id,
                            url=ORIGIN + "/next")
        result = completed(action, after)
        self.assertFalse(self.channel.claim_action(
            {"action_id": action.action_id}, lease_id=self.lease)["duplicate"])
        self.assertFalse(self.channel.accept_result(
            result, lease_id=self.lease)["duplicate"])
        self.assertEqual(
            self.channel.wait_for_result(action.action_id, timeout_s=0).content_digest,
            result.content_digest,
        )
        self.assertNotIn(action.action_id, self.channel._results)
        self.assertTrue(self.channel.accept_result(
            result, lease_id=self.lease)["duplicate"])

        conflict = completed(action, after, elapsed_ms=8)
        with self.assertRaisesRegex(RemoteProtocolError, "conflicting result"):
            self.channel.accept_result(conflict, lease_id=self.lease)

    def test_principal_bound_session_requires_exact_runtime_binding(self):
        now = int(time.time() * 1000)
        expected = principal(now)
        registry = RemoteSessionRegistry()
        grant, channel = registry.create(
            allowed_origins=[ORIGIN], session_id="principal-session",
            principal=expected)

        self.assertIs(channel.principal, expected)
        self.assertEqual(grant.application_id, "twenty")
        self.assertEqual(grant.principal_digest, expected.binding_digest)
        self.assertEqual(grant.expires_at, expected.expires_at)
        self.assertNotIn(expected.actor_id, repr(grant))
        with self.assertRaisesRegex(RemoteProtocolError, "principal binding") as missing:
            channel.accept_hello(hello(grant.session_id))
        self.assertEqual(missing.exception.code, "principal_mismatch")
        with self.assertRaisesRegex(RemoteProtocolError, "principal binding"):
            channel.accept_hello(hello(
                grant.session_id, application_id="twenty",
                principal_digest="0" * 64))
        reply = channel.accept_hello(hello(
            grant.session_id, application_id="twenty",
            principal_digest=expected.binding_digest))
        self.assertTrue(reply["lease_id"])

    def test_trusted_idle_renewal_extends_only_the_same_authority(self):
        initial = principal(1_000)
        with mock.patch("web_operation_agent.transport.channel._now_ms", return_value=2_000), \
             mock.patch("web_operation_agent.transport.registry._now_ms", return_value=2_000):
            registry = RemoteSessionRegistry(limits=RemoteSessionLimits(session_ttl_s=300))
            _grant, channel = registry.create(
                allowed_origins=[ORIGIN], session_id="renewed-session", principal=initial)
            renewed = dataclasses.replace(initial, issued_at=2_000, expires_at=122_000)
            channel.renew_principal(renewed)
            self.assertIs(channel.principal, renewed)
            self.assertEqual(channel.expires_at, 122_000)
            with self.assertRaisesRegex(Exception, "changed the bound authority"):
                channel.renew_principal(dataclasses.replace(
                    renewed, actor_id="actor-2", expires_at=182_000))

    def test_principal_renewal_refuses_an_active_turn(self):
        now = int(time.time() * 1000)
        initial = principal(now)
        registry = RemoteSessionRegistry()
        grant, channel = registry.create(
            allowed_origins=[ORIGIN], session_id="busy-renewal", principal=initial)
        lease = channel.accept_hello(hello(
            grant.session_id, application_id="twenty",
            principal_digest=initial.binding_digest))["lease_id"]
        channel.accept_message({"message_id": "turn-1", "text": "list companies",
                                "page_url": ORIGIN + "/start"}, lease_id=lease)
        channel.wait_for_message(timeout_s=0)
        with self.assertRaisesRegex(RemoteProtocolError, "idle session") as caught:
            channel.renew_principal(dataclasses.replace(
                initial, issued_at=now, expires_at=now + 120_000))
        self.assertEqual(caught.exception.code, "session_busy_turn")

    def test_unbound_session_rejects_a_runtime_declared_principal(self):
        with self.assertRaisesRegex(RemoteProtocolError, "no principal") as caught:
            self.channel.accept_hello(dataclasses.replace(
                hello(self.grant.session_id, runtime_instance_id="another-document"),
                application_id="twenty", principal_digest="0" * 64))
        self.assertEqual(caught.exception.code, "principal_mismatch")

    def test_registry_retirement_revokes_capability_and_releases_state(self):
        registry = RemoteSessionRegistry()
        grant, channel = registry.create(
            allowed_origins=[ORIGIN], session_id="retired-session")
        self.assertEqual(registry.session_count, 1)
        self.assertTrue(registry.retire(grant.session_id, "test_retired"))
        self.assertEqual(registry.session_count, 0)
        self.assertFalse(registry.retire(grant.session_id))
        with self.assertRaisesRegex(RemoteProtocolError, "unknown session"):
            registry.authenticate(grant.session_id, grant.client_token, ORIGIN)
        with self.assertRaises(RemoteSessionClosed):
            channel.validate_live()

    def test_unbound_session_ttl_revokes_capability_and_registry_state(self):
        limits = RemoteSessionLimits(session_ttl_s=60)
        with mock.patch("web_operation_agent.transport.channel._now_ms", return_value=1_000):
            registry = RemoteSessionRegistry(limits=limits)
            grant, channel = registry.create(
                allowed_origins=[ORIGIN], session_id="ttl-session")
        self.assertEqual(grant.expires_at, 0)
        self.assertEqual(grant.session_expires_at, 61_000)

        with mock.patch("web_operation_agent.transport.channel._now_ms", return_value=61_000):
            self.assertTrue(channel.message_cancelled("not-yet-cancelled"))
            with self.assertRaisesRegex(RemoteSessionClosed, "expired"):
                registry.authenticate(
                    grant.session_id, grant.client_token, ORIGIN)
            self.assertEqual(registry.session_count, 0)
            with self.assertRaises(RemoteSessionClosed):
                channel.validate_live()

    def test_a_waiting_worker_survives_a_session_that_outlives_its_own_wait(self):
        """P7.5 CR-44: the parked worker must not end when the session gets longer.

        The user submitted a question after an idle stretch and nothing happened at all --
        the panel showed the question and stayed on it. The turn worker had parked here
        waiting for the next message, with the session's remaining lifetime as its timeout.
        A renewal then extended that lifetime, the old timeout fired anyway, `TimeoutError`
        ended the worker thread, and the session went on accepting messages with nobody
        left to run them.
        """
        now = int(time.time() * 1000)
        initial = principal(now)
        registry = RemoteSessionRegistry()
        grant, channel = registry.create(
            allowed_origins=[ORIGIN], session_id="outlived-wait", principal=initial)
        lease = channel.accept_hello(hello(
            grant.session_id, application_id="twenty",
            principal_digest=initial.binding_digest))["lease_id"]
        # The worker parks while the session has a short life left in front of it.
        channel.expires_at = int(time.time() * 1000) + 300
        taken: list = []
        worker = threading.Thread(
            target=lambda: taken.append(channel.wait_for_message(timeout_s=None)),
            daemon=True)
        worker.start()
        time.sleep(0.1)
        # The renewal the loader makes on the person's behalf: the session now lives well
        # past the point this wait was told to end at.
        channel.renew_principal(dataclasses.replace(
            initial, issued_at=now, expires_at=now + 600_000))
        time.sleep(0.4)  # past the lifetime the wait began with
        self.assertTrue(worker.is_alive(), "the worker ended while its session lived on")
        channel.accept_message({"message_id": "late-1", "text": "P75 的电话是什么",
                                "page_url": ORIGIN + "/start"}, lease_id=lease)
        worker.join(timeout=5)
        self.assertEqual([message["message_id"] for message in taken], ["late-1"])

    def test_stopping_a_message_no_turn_has_started_ends_it_there_and_then(self):
        """P7.5 CR-44: the stop control has to end something that never began.

        With the worker gone, the person pressed stop. The session recorded `cancelling`
        and waited for the turn to wind down -- but no turn had picked the message up, so
        the terminal event never came and the panel sat on "正在停止" with its composer
        locked. A queued message is cancelled by removing it, not by asking it to stop.
        """
        registry = RemoteSessionRegistry()
        grant, channel = registry.create(
            allowed_origins=[ORIGIN], session_id="stop-before-start")
        lease = channel.accept_hello(hello(grant.session_id))["lease_id"]
        channel.accept_message({"message_id": "queued-1", "text": "P75 的电话是什么",
                                "page_url": ORIGIN + "/start"}, lease_id=lease)
        channel.accept_cancellation(CancellationRequest(
            schema_version=SCHEMA_VERSIONS["CancellationRequest"],
            session_id=grant.session_id, message_id="queued-1"), lease_id=lease)
        states = [event.payload.get("state") for event in
                  channel.events_after("", lease_id=lease, wait_s=0)
                  if event.kind == "progress"]
        self.assertEqual(states[-1], "cancelled")
        self.assertTrue(channel.message_cancelled("queued-1"))
        # And it is gone: no worker picks it up later and answers a question the person
        # already stopped.
        with self.assertRaises(TimeoutError):
            channel.wait_for_message(timeout_s=0.2)

    def test_message_queue_applies_backpressure_without_losing_the_first_message(self):
        registry = RemoteSessionRegistry(limits=RemoteSessionLimits(
            max_messages=2, max_queued_messages=1))
        grant, channel = registry.create(
            allowed_origins=[ORIGIN], session_id="backpressure-session")
        lease = channel.accept_hello(hello(grant.session_id))["lease_id"]
        first = {"message_id": "m-1", "text": "First", "page_url": ORIGIN + "/start"}
        second = {"message_id": "m-2", "text": "Second", "page_url": ORIGIN + "/start"}

        channel.accept_message(first, lease_id=lease)
        with self.assertRaisesRegex(RemoteProtocolError, "queue is full") as caught:
            channel.accept_message(second, lease_id=lease)
        self.assertEqual(caught.exception.code, "backpressure")
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(channel.wait_for_message(timeout_s=0), first)
        channel.complete_message("m-1")
        self.assertFalse(channel.accept_message(second, lease_id=lease)["duplicate"])

    def test_message_lifetime_capacity_ends_the_session(self):
        registry = RemoteSessionRegistry(limits=RemoteSessionLimits(
            max_messages=1, max_queued_messages=1))
        grant, channel = registry.create(
            allowed_origins=[ORIGIN], session_id="message-capacity-session")
        lease = channel.accept_hello(hello(grant.session_id))["lease_id"]
        first = {"message_id": "m-1", "text": "First", "page_url": ORIGIN + "/start"}
        channel.accept_message(first, lease_id=lease)
        channel.wait_for_message(timeout_s=0)
        channel.complete_message("m-1")

        with self.assertRaisesRegex(RemoteSessionClosed, "message capacity"):
            channel.accept_message(
                {"message_id": "m-2", "text": "Second",
                 "page_url": ORIGIN + "/start"},
                lease_id=lease,
            )

    def test_event_capacity_reserves_a_terminal_event(self):
        registry = RemoteSessionRegistry(limits=RemoteSessionLimits(max_events=3))
        grant, channel = registry.create(
            allowed_origins=[ORIGIN], session_id="event-capacity-session")
        lease = channel.accept_hello(hello(grant.session_id))["lease_id"]
        channel.publish("progress", {"state": "working"})

        with self.assertRaisesRegex(RemoteSessionClosed, "event capacity"):
            channel.publish("progress", {"state": "still_working"})
        events = channel.events_after("", lease_id=lease)
        self.assertEqual([event.sequence for event in events], [1, 2, 3])
        self.assertEqual(events[-1].kind, "terminal")
        self.assertEqual(events[-1].payload["reason"], "event_capacity_exhausted")

    def test_session_limit_values_are_strict_and_coherent(self):
        for changes in (
                {"session_ttl_s": 0}, {"max_events": 2},
                {"max_messages": True}, {"max_runtime_instances": 10_001},
                {"max_messages": 1, "max_queued_messages": 2}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                RemoteSessionLimits(**changes)

    def test_oversized_event_is_rejected_without_poisoning_the_journal(self):
        with self.assertRaisesRegex(RemoteProtocolError, "256 KiB") as caught:
            self.channel.publish("progress", {"state": "working", "detail": "x" * 300_000})
        self.assertEqual(caught.exception.code, "payload_too_large")
        event = self.channel.publish("progress", {"state": "working"})
        self.assertEqual(event.sequence, 2)

    def test_oversized_action_does_not_leave_a_phantom_in_flight_action(self):
        command = ActionCommand(
            command_id="large-action", action="fill",
            args={"ref": "e0", "value": "x" * 300_000},
        )
        oversized = ActionEnvelope(
            schema_version=SCHEMA_VERSIONS["ActionEnvelope"],
            session_id=self.grant.session_id, turn_id="t-1",
            action_id="large-action",
            based_on_observation_id=self.first.observation_id,
            page_epoch=self.first.page_epoch,
            deadline=int(time.time() * 1000) + 5_000,
            command_digest=command.content_digest,
            command=command,
        )
        with self.assertRaisesRegex(RemoteProtocolError, "256 KiB"):
            self.channel.enqueue_action(oversized)
        self.assertIsNone(self.channel.pending_action)

        accepted = self.channel.enqueue_action(
            action_for(self.grant.session_id, self.first, action_id="small-action"))
        self.assertEqual(accepted.kind, "action")

    def test_message_identifiers_and_origins_are_bounded(self):
        with self.assertRaisesRegex(RemoteProtocolError, "message_id"):
            self.channel.accept_message({
                "message_id": "x" * 129,
                "text": "Task",
                "page_url": ORIGIN + "/start",
            }, lease_id=self.lease)
        # The refusal names what is wrong with it, rather than "not an absolute origin".
        with self.assertRaisesRegex(RemoteProtocolError, "user information"):
            RemoteSessionRegistry().create(
                allowed_origins=["http://user:password@app.test"],
                session_id="credential-origin",
            )

    def test_an_idle_host_wait_ends_at_the_session_ttl(self):
        registry = RemoteSessionRegistry(
            limits=RemoteSessionLimits(session_ttl_s=1))
        _, channel = registry.create(
            allowed_origins=[ORIGIN], session_id="idle-ttl-session")
        outcome = {}

        def wait() -> None:
            try:
                channel.wait_for_message(timeout_s=None)
            except Exception as exc:
                outcome["error"] = exc

        waiter = threading.Thread(target=wait, daemon=True)
        waiter.start()
        waiter.join(2)
        self.assertFalse(waiter.is_alive())
        self.assertIsInstance(outcome.get("error"), RemoteSessionClosed)

    def test_expired_principal_rejects_authenticated_and_server_operations(self):
        now = int(time.time() * 1000)
        expected = principal(now)
        registry = RemoteSessionRegistry()
        grant, channel = registry.create(
            allowed_origins=[ORIGIN], session_id="expiring-session",
            principal=expected)
        with mock.patch("web_operation_agent.transport.channel._now_ms",
                        return_value=expected.expires_at):
            with self.assertRaisesRegex(RemoteSessionClosed, "principal"):
                registry.authenticate(
                    grant.session_id, grant.client_token, ORIGIN)
            with self.assertRaisesRegex(RemoteSessionClosed, "principal"):
                channel.validate_live()

    def test_bound_approval_cannot_cross_principal_turn_or_operation(self):
        now = int(time.time() * 1000)
        expected = principal(now)
        registry = RemoteSessionRegistry()
        grant, channel = registry.create(
            allowed_origins=[ORIGIN], session_id="bound-approval",
            principal=expected)
        lease = channel.accept_hello(hello(
            grant.session_id, application_id="twenty",
            principal_digest=expected.binding_digest))["lease_id"]
        message = {"message_id": "message-1", "text": "Create Acme",
                   "page_url": ORIGIN + "/start"}
        channel.accept_message(message, lease_id=lease)
        channel.wait_for_message(timeout_s=0)
        context = OperationContext.create(
            expected, session_id=grant.session_id, turn_id="message-1",
            operation_id="op-create-1", deadline_ms=now + 30_000, now_ms=now)
        request = ApprovalRequest(
            1, "approval-bound", "Create company", ("Acme",),
            context.expires_at, expected.binding_digest,
            context.turn_id, context.operation_id)
        channel.request_approval(request)

        with self.assertRaisesRegex(RemoteProtocolError, "operation context") as mismatch:
            channel.accept_approval(ApprovalDecision(
                1, grant.session_id, request.approval_id, "approved"), lease_id=lease)
        self.assertEqual(mismatch.exception.code, "approval_context_mismatch")
        decision = ApprovalDecision(
            1, grant.session_id, request.approval_id, "approved",
            request.principal_digest, request.turn_id, request.operation_id)
        self.assertFalse(channel.accept_approval(decision, lease_id=lease)["duplicate"])

    def test_approval_is_user_visible_idempotent_and_bound_to_this_session(self):
        request = ApprovalRequest(
            schema_version=1, approval_id="approval-1",
            effect="Accept supplier quote", targets=("Quote Q-1042",),
            expires_at=int(time.time() * 1000) + 5000)
        event = self.channel.request_approval(request)
        self.assertEqual(event.kind, "approval_required")
        wire = json.dumps(event.to_json())
        self.assertIn("Accept supplier quote", wire)
        for hidden in ("ControlPlan", "createQuote", "permit"):
            self.assertNotIn(hidden, wire)
        self.assertEqual(self.channel.request_approval(request), event)

        decision = ApprovalDecision(
            schema_version=1, session_id=self.grant.session_id,
            approval_id=request.approval_id, decision="approved")
        reply = self.channel.accept_approval(decision, lease_id=self.lease)
        self.assertFalse(reply["duplicate"])
        self.assertTrue(self.channel.accept_approval(
            decision, lease_id=self.lease)["duplicate"])
        self.assertEqual(
            self.channel.wait_for_approval(request.approval_id, timeout_s=0), decision)
        conflict = dataclasses.replace(decision, decision="denied")
        with self.assertRaisesRegex(RemoteProtocolError, "conflicts"):
            self.channel.accept_approval(conflict, lease_id=self.lease)

    def test_approval_unknown_expired_and_concurrent_requests_fail_closed(self):
        now = int(time.time() * 1000)
        first = ApprovalRequest(1, "approval-1", "Change a record", ("Record 1",), now + 5000)
        self.channel.request_approval(first)
        with self.assertRaisesRegex(RemoteProtocolError, "another approval"):
            self.channel.request_approval(
                ApprovalRequest(1, "approval-2", "Change another", ("Record 2",), now + 5000))
        unknown = ApprovalDecision(1, self.grant.session_id, "missing", "denied")
        with self.assertRaisesRegex(RemoteProtocolError, "unknown"):
            self.channel.accept_approval(unknown, lease_id=self.lease)

        registry = RemoteSessionRegistry()
        grant, channel = registry.create(allowed_origins=[ORIGIN], session_id="expiry-session")
        lease = channel.accept_hello(hello(grant.session_id))["lease_id"]
        expired = ApprovalRequest(1, "late", "Change a record", ("Record 1",), now + 1)
        with mock.patch("web_operation_agent.transport.channel._now_ms", return_value=now):
            channel.request_approval(expired)
        decision = ApprovalDecision(1, grant.session_id, "late", "approved")
        with mock.patch("web_operation_agent.transport.channel._now_ms", return_value=now + 2):
            with self.assertRaisesRegex(RemoteProtocolError, "expired"):
                channel.accept_approval(decision, lease_id=lease)

    def test_an_expired_confirmation_stops_being_offered_to_the_panel(self):
        """P7.5 J3.05: the card came back after the deadline and could only be refused.

        The waiting turn gave up at the deadline, but the session went on naming that
        approval as the pending one. Every later projection of the timeline handed the
        panel the same card -- so it reappeared after a reload, the composer stayed
        blocked behind it, and answering it said the confirmation was no longer valid.
        """
        now = int(time.time() * 1000)
        registry = RemoteSessionRegistry()
        grant, channel = registry.create(
            allowed_origins=[ORIGIN], session_id="expired-card-session")
        lease = channel.accept_hello(hello(grant.session_id))["lease_id"]
        with mock.patch("web_operation_agent.transport.channel._now_ms", return_value=now):
            channel.request_approval(
                ApprovalRequest(1, "late-card", "Change a record", ("Record 1",), now + 40))
            self.assertEqual(
                channel.panel_timeline(lease_id=lease)["pending_approval"]["approval_id"],
                "late-card")
        with mock.patch("web_operation_agent.transport.channel._now_ms", return_value=now + 100):
            with self.assertRaisesRegex(RemoteProtocolError, "expired"):
                channel.wait_for_approval("late-card", timeout_s=0.2)
        self.assertIsNone(channel.panel_timeline(lease_id=lease)["pending_approval"])
        # And a second request can be raised, because the session is no longer holding one.
        channel.request_approval(
            ApprovalRequest(1, "next-card", "Change another", ("Record 2",),
                            int(time.time() * 1000) + 5_000))

    def test_approval_card_cannot_carry_a_credential(self):
        request = ApprovalRequest(
            1, "approval-secret", "Open callback",
            ("/callback?access_token=raw-secret",), int(time.time() * 1000) + 5000)
        with self.assertRaisesRegex(RemoteProtocolError, "credential"):
            self.channel.request_approval(request)

    def test_cancellation_rejects_an_unclaimed_action_and_wakes_the_server(self):
        message = {"message_id": "message-1", "text": "Open the next page",
                   "page_url": ORIGIN + "/start"}
        self.channel.accept_message(message, lease_id=self.lease)
        self.assertEqual(self.channel.wait_for_message(timeout_s=0), message)
        action = dataclasses.replace(
            action_for(self.grant.session_id, self.first), turn_id="message-1")
        self.channel.enqueue_action(action)

        request = CancellationRequest(1, self.grant.session_id, "message-1")
        reply = self.channel.accept_cancellation(request, lease_id=self.lease)
        self.assertEqual(reply, {"accepted": True, "duplicate": False})
        self.assertTrue(self.channel.message_cancelled("message-1"))
        with self.assertRaisesRegex(RemoteProtocolError, "before browser execution") as caught:
            self.channel.wait_for_result(action.action_id, timeout_s=0)
        self.assertEqual(caught.exception.code, "turn_cancelled")
        with self.assertRaisesRegex(RemoteProtocolError, "before action claim"):
            self.channel.claim_action(
                {"action_id": action.action_id}, lease_id=self.lease)
        self.assertIsNone(self.channel.pending_action)
        self.assertTrue(self.channel.accept_cancellation(
            request, lease_id=self.lease)["duplicate"])

        self.channel.complete_message("message-1")
        self.assertEqual(self.channel.accept_cancellation(
            request, lease_id=self.lease),
            {"accepted": False, "duplicate": False, "completed": True})

    def test_remote_browser_surfaces_pre_claim_cancellation_as_typed_terminal(self):
        message = {"message_id": "message-browser", "text": "Open the next page",
                   "page_url": ORIGIN + "/start"}
        self.channel.accept_message(message, lease_id=self.lease)
        self.channel.wait_for_message(timeout_s=0)
        browser = RemoteBrowserClient(
            self.channel, timeout_s=2, turn_id="message-browser")
        browser.start()
        answer = {}

        def run_action():
            try:
                browser.execute("click", {"ref": "e0"})
            except Exception as exc:
                answer["error"] = exc

        worker = threading.Thread(target=run_action)
        worker.start()
        self.await_action_event()
        self.channel.accept_cancellation(
            CancellationRequest(1, self.grant.session_id, "message-browser"),
            lease_id=self.lease)
        worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertIsInstance(answer.get("error"), TurnCancelled)

    def test_cancellation_denies_a_pending_approval_but_not_a_claimed_action(self):
        message = {"message_id": "message-2", "text": "Update the quote",
                   "page_url": ORIGIN + "/start"}
        self.channel.accept_message(message, lease_id=self.lease)
        self.channel.wait_for_message(timeout_s=0)
        approval = ApprovalRequest(
            1, "approval-cancel", "Update quote", ("Quote Q-1042",),
            int(time.time() * 1000) + 5000)
        self.channel.request_approval(approval)
        request = CancellationRequest(1, self.grant.session_id, "message-2")
        self.channel.accept_cancellation(request, lease_id=self.lease)
        decision = self.channel.wait_for_approval(approval.approval_id, timeout_s=0)
        self.assertEqual(decision.decision, "denied")

        self.channel.complete_message("message-2")
        message = {"message_id": "message-3", "text": "Open the next page",
                   "page_url": ORIGIN + "/start"}
        self.channel.accept_message(message, lease_id=self.lease)
        self.channel.wait_for_message(timeout_s=0)
        action = dataclasses.replace(
            action_for(self.grant.session_id, self.first, action_id="a-claimed"),
            turn_id="message-3")
        self.channel.enqueue_action(action)
        self.channel.claim_action({"action_id": action.action_id}, lease_id=self.lease)
        self.channel.accept_cancellation(
            CancellationRequest(1, self.grant.session_id, "message-3"),
            lease_id=self.lease)
        after = observation(self.grant.session_id, 2, caused_by=action.action_id,
                            url=ORIGIN + "/next")
        expected = completed(action, after)
        self.channel.accept_result(expected, lease_id=self.lease)
        self.assertEqual(
            self.channel.wait_for_result(action.action_id, timeout_s=0), expected)

    def test_cancellation_requires_current_lease_known_message_and_same_session(self):
        message = {"message_id": "message-security", "text": "Open next",
                   "page_url": ORIGIN + "/start"}
        self.channel.accept_message(message, lease_id=self.lease)
        with self.assertRaisesRegex(RemoteProtocolError, "unknown message"):
            self.channel.accept_cancellation(
                CancellationRequest(1, self.grant.session_id, "missing"),
                lease_id=self.lease)
        with self.assertRaisesRegex(RemoteProtocolError, "another session"):
            self.channel.accept_cancellation(
                CancellationRequest(1, "other-session", "message-security"),
                lease_id=self.lease)
        new_lease = self.channel.accept_hello(hello(
            self.grant.session_id, runtime_instance_id="runtime-document-2"))["lease_id"]
        with self.assertRaisesRegex(RemoteProtocolError, "lease"):
            self.channel.accept_cancellation(
                CancellationRequest(1, self.grant.session_id, "message-security"),
                lease_id=self.lease)
        self.assertTrue(self.channel.accept_cancellation(
            CancellationRequest(1, self.grant.session_id, "message-security"),
            lease_id=new_lease)["accepted"])

    def test_stale_duplicate_and_second_in_flight_fail_closed(self):
        self.assertTrue(self.channel.accept_observation(
            self.first, lease_id=self.lease)["duplicate"])
        changed = observation(self.grant.session_id, 1)
        changed.observation.title = "different bytes"
        with self.assertRaisesRegex(RemoteProtocolError, "different content"):
            self.channel.accept_observation(changed, lease_id=self.lease)

        first_action = action_for(self.grant.session_id, self.first)
        self.channel.enqueue_action(first_action)
        with self.assertRaisesRegex(RemoteProtocolError, "already in flight"):
            self.channel.enqueue_action(action_for(self.grant.session_id, self.first,
                                                   action_id="a-2"))

    def test_old_observation_and_expired_action_are_never_enqueued(self):
        newer = observation(self.grant.session_id, 2)
        self.channel.accept_observation(newer, lease_id=self.lease)
        with self.assertRaisesRegex(RemoteProtocolError, "stale observation"):
            self.channel.enqueue_action(action_for(self.grant.session_id, self.first))
        with self.assertRaisesRegex(RemoteProtocolError, "expired"):
            self.channel.enqueue_action(action_for(
                self.grant.session_id, newer, deadline=int(time.time() * 1000) - 1))
        self.assertEqual(self.channel.events_after(
            "e00000001", lease_id=self.lease), [])

    def test_remote_browser_adapter_roundtrip_unblocks_on_fresh_result(self):
        browser = RemoteBrowserClient(self.channel, timeout_s=2)
        self.assertIsInstance(browser, BrowserExecutor)
        self.assertEqual(browser.start()["url"], self.first.observation.url)
        answer = {}

        def run_action():
            try:
                answer["receipt"] = browser.execute("click", {"ref": "e0"},
                                                    expect_navigation=True)
            except Exception as exc:  # surfaced in the assertion thread
                answer["error"] = exc

        worker = threading.Thread(target=run_action)
        worker.start()
        envelope = self.await_action_event().action()
        self.assertEqual(envelope.command, ActionCommand(
            command_id=envelope.action_id,
            action="click",
            args={"ref": "e0"},
            expect_navigation=True,
        ))
        self.assertEqual(envelope.command_digest, envelope.command.content_digest)
        self.channel.claim_action({"action_id": envelope.action_id},
                                  lease_id=self.lease)
        after = observation(self.grant.session_id, 2, caused_by=envelope.action_id,
                            url=ORIGIN + "/next")
        self.channel.accept_result(completed(envelope, after), lease_id=self.lease)
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertNotIn("error", answer)
        self.assertEqual(answer["receipt"].observation.url, ORIGIN + "/next")

    def test_async_observation_is_feedback_not_a_ref_rebinding(self):
        browser = RemoteBrowserClient(self.channel, timeout_s=2)
        self.assertEqual(browser.start()["url"], self.first.observation.url)
        newer = observation(self.grant.session_id, 2, url=ORIGIN + "/new-page")
        self.channel.accept_observation(newer, lease_id=self.lease)

        secret = "predispatch-fill-secret"
        stale = browser.execute("fill", {"ref": "e0", "value": secret})
        self.assertFalse(stale.ok)
        self.assertEqual(stale.error_code, "stale_observation")
        self.assertEqual(stale.epoch_before, 1)
        self.assertEqual(stale.observation.url, ORIGIN + "/new-page")
        self.assertEqual(stale.args_echo["value"], "[redacted]")
        self.assertNotIn(secret, json.dumps(stale.to_json()))
        self.assertIsNone(self.channel.pending_action)
        self.assertEqual(self.channel.events_after(
            "e00000001", lease_id=self.lease), [])

        answer = {}

        def run_replanned_action():
            try:
                answer["receipt"] = browser.execute(
                    "click", {"ref": "e0"}, expect_navigation=True)
            except Exception as exc:
                answer["error"] = exc

        worker = threading.Thread(target=run_replanned_action)
        worker.start()
        event = self.channel.events_after(
            "e00000001", lease_id=self.lease, wait_s=1)[0]
        rebound = event.action()
        self.assertEqual(rebound.based_on_observation_id, newer.observation_id)
        self.assertEqual(rebound.page_epoch, newer.page_epoch)
        self.channel.claim_action(
            {"action_id": rebound.action_id}, lease_id=self.lease)
        after = observation(self.grant.session_id, 3,
                            caused_by=rebound.action_id, url=ORIGIN + "/done")
        self.channel.accept_result(
            completed(rebound, after, url_before=newer.observation.url),
            lease_id=self.lease)
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertNotIn("error", answer)

    def test_receipt_cannot_change_command_arguments_or_start_url(self):
        action = action_for(self.grant.session_id, self.first)
        self.channel.enqueue_action(action)
        self.channel.claim_action({"action_id": action.action_id}, lease_id=self.lease)
        after = observation(self.grant.session_id, 2, caused_by=action.action_id,
                            url=ORIGIN + "/next")
        valid = completed(action, after)
        bad_receipts = (
            dataclasses.replace(valid.receipt, args_echo={"ref": "e99"}),
            dataclasses.replace(valid.receipt, url_before=ORIGIN + "/invented"),
        )
        for receipt in bad_receipts:
            with self.subTest(receipt=receipt), self.assertRaises(RemoteProtocolError):
                self.channel.accept_result(
                    dataclasses.replace(valid, receipt=receipt), lease_id=self.lease)

    def test_remote_pre_dispatch_code_and_redaction_reach_agent_unchanged(self):
        browser = RemoteBrowserClient(self.channel, timeout_s=2)
        browser.start()
        answer = {}

        def run_action():
            try:
                answer["receipt"] = browser.execute(
                    "fill", {"ref": "e0", "value": "must-not-return"})
            except Exception as exc:
                answer["error"] = exc

        worker = threading.Thread(target=run_action)
        worker.start()
        envelope = self.await_action_event().action()
        after = observation(self.grant.session_id, 2, caused_by=envelope.action_id)
        rejected = ActionResultEnvelope(
            schema_version=SCHEMA_VERSIONS["ActionResultEnvelope"],
            session_id=envelope.session_id, turn_id=envelope.turn_id,
            action_id=envelope.action_id,
            based_on_observation_id=envelope.based_on_observation_id,
            page_epoch=envelope.page_epoch, command_digest=envelope.command_digest,
            status="rejected", observation_after=after, receipt=None,
            error_code="deadline_expired", error_detail="not dispatched",
        )
        self.channel.accept_result(rejected, lease_id=self.lease)
        worker.join(timeout=2)
        self.assertNotIn("error", answer)
        self.assertEqual(answer["receipt"].error_code, "deadline_expired")
        self.assertEqual(answer["receipt"].args_echo["value"], "[redacted]")
        self.assertNotIn("must-not-return", json.dumps(answer["receipt"].to_json()))

    def test_indeterminate_result_is_transport_failure_and_not_retried(self):
        browser = RemoteBrowserClient(self.channel, timeout_s=2)
        browser.start()
        answer = {}

        def run_action():
            try:
                browser.execute("click", {"ref": "e0"})
            except Exception as exc:
                answer["error"] = exc

        worker = threading.Thread(target=run_action)
        worker.start()
        envelope = self.await_action_event().action()
        self.channel.claim_action({"action_id": envelope.action_id},
                                  lease_id=self.lease)
        result = ActionResultEnvelope(
            schema_version=SCHEMA_VERSIONS["ActionResultEnvelope"],
            session_id=envelope.session_id, turn_id=envelope.turn_id,
            action_id=envelope.action_id,
            based_on_observation_id=envelope.based_on_observation_id,
            page_epoch=envelope.page_epoch,
            command_digest=envelope.command_digest, status="indeterminate",
            observation_after=None, error_code="execution_state_unknown",
            error_detail="the old document ended before it could record a receipt",
        )
        self.channel.accept_result(result, lease_id=self.lease)
        worker.join(timeout=2)
        self.assertIsInstance(answer.get("error"), TransportError)
        action_events = [event for event in self.channel.events_after(
            "e00000001", lease_id=self.lease)
                         if event.kind == "action"]
        self.assertEqual(len(action_events), 1)

    def test_runtime_takeover_preserves_old_claim_for_same_client_result(self):
        action = action_for(self.grant.session_id, self.first)
        self.channel.enqueue_action(action)
        first_claim = self.channel.claim_action(
            {"action_id": action.action_id}, lease_id=self.lease)
        self.assertFalse(first_claim["duplicate"])
        self.assertTrue(self.channel.claim_action(
            {"action_id": action.action_id}, lease_id=self.lease)["duplicate"])

        takeover = self.channel.accept_hello(hello(
            self.grant.session_id, cursor="e00000002",
            runtime_instance_id="runtime-document-2"))
        new_lease = takeover["lease_id"]
        self.assertNotEqual(new_lease, self.lease)
        with self.assertRaisesRegex(RemoteProtocolError, "replaced"):
            self.channel.validate_active_lease(self.lease)
        refused_claim = self.channel.claim_action(
            {"action_id": action.action_id}, lease_id=new_lease)
        self.assertEqual(refused_claim,
                         {"claimed": False, "reason": "already_claimed"})
        with self.assertRaisesRegex(RemoteProtocolError, "replaced"):
            self.channel.accept_hello(hello(
                self.grant.session_id,
                runtime_instance_id="runtime-document-1"))

        after = observation(self.grant.session_id, 2, caused_by=action.action_id,
                            url=ORIGIN + "/next")
        reply = self.channel.accept_result(completed(action, after),
                                           lease_id=new_lease)
        self.assertFalse(reply["duplicate"])

    def test_same_lease_claim_retry_survives_deadline_after_response_loss(self):
        action = action_for(self.grant.session_id, self.first)
        self.channel.enqueue_action(action)
        self.assertFalse(self.channel.claim_action(
            {"action_id": action.action_id}, lease_id=self.lease)["duplicate"])
        with mock.patch("web_operation_agent.transport.channel._now_ms",
                        return_value=action.deadline + 1):
            retry = self.channel.claim_action(
                {"action_id": action.action_id}, lease_id=self.lease)
        self.assertEqual(retry, {"claimed": True, "duplicate": True})

    def test_hello_rejects_cursor_ahead_of_server_journal(self):
        repeated = self.channel.accept_hello(hello(self.grant.session_id))
        self.assertEqual(repeated["lease_id"], self.lease)
        with self.assertRaisesRegex(RemoteProtocolError, "ahead"):
            self.channel.accept_hello(hello(
                self.grant.session_id, cursor="e00000002"))

    def test_channel_detaches_mutable_wire_values(self):
        action = action_for(self.grant.session_id, self.first)
        returned_event = self.channel.enqueue_action(action)
        action.command.args["ref"] = "caller-mutated"
        returned_event.payload["command"]["args"]["ref"] = "return-mutated"
        self.first.observation.elements[0].attrs["href"] = "/caller-mutated"

        pending = self.channel.pending_action
        self.assertEqual(pending.command.args["ref"], "e0")
        replay = self.channel.events_after(
            "e00000001", lease_id=self.lease)
        self.assertEqual(replay[0].payload["command"]["args"]["ref"], "e0")
        self.assertEqual(
            self.channel.latest_observation_envelope.observation.elements[0].attrs["href"],
            "/next",
        )

    def test_arrived_result_wins_close_race(self):
        action = action_for(self.grant.session_id, self.first)
        self.channel.enqueue_action(action)
        self.channel.claim_action({"action_id": action.action_id},
                                  lease_id=self.lease)
        after = observation(self.grant.session_id, 2, caused_by=action.action_id,
                            url=ORIGIN + "/next")
        expected = completed(action, after)
        self.channel.accept_result(expected, lease_id=self.lease)
        self.channel.close()
        actual = self.channel.wait_for_result(action.action_id, timeout_s=0)
        self.assertEqual(actual.to_json(), expected.to_json())
        self.assertTrue(self.channel.accept_result(
            expected, lease_id=self.lease)["duplicate"])

        conflict = completed(action, after, elapsed_ms=8)
        with self.assertRaisesRegex(RemoteProtocolError, "conflicting result"):
            self.channel.accept_result(conflict, lease_id=self.lease)
        wrong_command = expected.to_json()
        wrong_command["command_digest"] = "0" * 64
        with self.assertRaisesRegex(RemoteProtocolError, "command_digest"):
            self.channel.accept_result(
                ActionResultEnvelope.from_json(wrong_command), lease_id=self.lease)

    def test_terminal_event_remains_replayable_after_close(self):
        self.channel.close("done")
        events = self.channel.events_after(
            "e00000001", lease_id=self.lease)
        self.assertEqual([event.kind for event in events], ["terminal"])
        self.assertEqual(events[0].payload["reason"], "done")
        with self.assertRaisesRegex(RemoteProtocolError, "closed"):
            self.channel.events_after(events[0].event_id, lease_id=self.lease)


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.registry = RemoteSessionRegistry()
        self.grant, self.channel = self.registry.create(allowed_origins=[ORIGIN],
                                                        session_id="http-session")
        self.gateway = EmbeddedGateway(self.registry, sse_wait_s=0.05).start()
        self.lease = ""

    def tearDown(self):
        self.gateway.close()

    def request(self, method: str, path: str, body=None, *, token: str | None = None,
                origin: str = ORIGIN, headers=None, lease=DEFAULT_LEASE):
        payload = None if body is None else json.dumps(body).encode()
        request_headers = {"Origin": origin,
                           "Authorization": "Bearer " + (token or self.grant.client_token)}
        selected_lease = self.lease if lease is DEFAULT_LEASE else lease
        if selected_lease:
            request_headers["X-WOA-Lease"] = selected_lease
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        request = urllib.request.Request(self.gateway.base_url + path, data=payload,
                                         method=method, headers=request_headers)
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def test_authenticated_http_sse_action_roundtrip_and_duplicate_result(self):
        sid = self.grant.session_id
        status, headers, raw_hello = self.request(
            "POST", f"/v1/sessions/{sid}/hello", hello(sid).to_json())
        self.assertEqual(status, 200)
        self.assertEqual(headers["Access-Control-Allow-Origin"], ORIGIN)
        self.lease = json.loads(raw_hello)["lease_id"]
        first = observation(sid, 1)
        self.assertEqual(self.request("POST", f"/v1/sessions/{sid}/observations",
                                      first.to_json())[0], 200)

        action = action_for(sid, first)
        self.channel.enqueue_action(action)
        status, headers, raw = self.request(
            "GET", f"/v1/sessions/{sid}/events", headers={"Last-Event-Id": "e00000001"})
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/event-stream"))
        self.assertIn(f"id: e00000002".encode(), raw)
        self.assertIn(action.action_id.encode(), raw)
        claim_reply = json.loads(self.request(
            "POST", f"/v1/sessions/{sid}/claims",
            {"action_id": action.action_id})[2])
        self.assertFalse(claim_reply["duplicate"])

        after = observation(sid, 2, caused_by=action.action_id, url=ORIGIN + "/next")
        result = completed(action, after)
        first_reply = json.loads(self.request("POST", f"/v1/sessions/{sid}/results",
                                              result.to_json())[2])
        retry_reply = json.loads(self.request("POST", f"/v1/sessions/{sid}/results",
                                              result.to_json())[2])
        self.assertFalse(first_reply["duplicate"])
        self.assertTrue(retry_reply["duplicate"])

    def test_authenticated_approval_endpoint_roundtrip(self):
        sid = self.grant.session_id
        _, _, raw_hello = self.request(
            "POST", f"/v1/sessions/{sid}/hello", hello(sid).to_json())
        self.lease = json.loads(raw_hello)["lease_id"]
        request = ApprovalRequest(
            1, "approval-http", "Update the quote", ("Quote Q-1042",),
            int(time.time() * 1000) + 5000)
        self.channel.request_approval(request)
        body = ApprovalDecision(1, sid, request.approval_id, "approved").to_json()
        status, _, raw = self.request(
            "POST", f"/v1/sessions/{sid}/approvals", body)
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(raw)["duplicate"])
        status, _, raw = self.request(
            "POST", f"/v1/sessions/{sid}/approvals", body)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw)["duplicate"])

    def test_authenticated_cancellation_endpoint_is_message_bound(self):
        sid = self.grant.session_id
        _, _, raw_hello = self.request(
            "POST", f"/v1/sessions/{sid}/hello", hello(sid).to_json())
        self.lease = json.loads(raw_hello)["lease_id"]
        message = {"message_id": "message-http", "text": "Open next",
                   "page_url": ORIGIN + "/start"}
        self.assertEqual(self.request(
            "POST", f"/v1/sessions/{sid}/messages", message)[0], 200)
        self.channel.wait_for_message(timeout_s=0)
        body = CancellationRequest(1, sid, "message-http").to_json()
        status, _, raw = self.request(
            "POST", f"/v1/sessions/{sid}/cancellations", body)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"accepted": True, "duplicate": False})
        self.assertTrue(self.channel.message_cancelled("message-http"))

    def test_token_origin_cors_and_static_runtime_boundary(self):
        sid = self.grant.session_id
        self.assertEqual(self.request("POST", f"/v1/sessions/{sid}/hello",
                                      hello(sid).to_json(), token="wrong")[0], 401)
        self.assertEqual(self.request("POST", f"/v1/sessions/{sid}/hello",
                                      hello(sid).to_json(), origin="http://evil.test")[0], 403)
        # `assistant.js` and the panel are build artifacts; where one is absent the gateway
        # names the prerequisite instead of serving whatever happens to be in the directory.
        status, headers, body = self.request("GET", "/v1/runtime/assistant.js")
        if status == 200:
            self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
            self.assertIn(b"class EmbeddedWebRuntime", body)
            self.assertNotIn(b"OPENAI_API_KEY", body)
            self.assertNotIn(b"ControlPlan", body)
        else:
            self.assertEqual(status, 404)
            self.assertIn(b"runtime_unavailable", body)
        status, headers, body = self.request("GET", "/v1/runtime/panel/panel.js")
        if status == 200:
            self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertIn("immutable", headers["Cache-Control"])
            self.assertNotIn(b"clientToken", body)
            self.assertNotIn(b"ControlPlan", body)
        else:
            self.assertEqual(status, 404)
            self.assertIn(b"panel_unavailable", body)
        # Path traversal out of the panel directory is refused by the manifest, not by
        # string surgery on the request path.
        self.assertEqual(
            self.request("GET", "/v1/runtime/panel/../../assistant.js")[0], 404)

    def test_an_unbuilt_runtime_is_named_rather_than_answered_with_an_error(self):
        """`assistant.js` is a build artifact, like the panel, and is missing the same way."""
        from pathlib import Path

        from web_operation_agent.transport import gateway as embedded_gateway
        missing = Path("/nonexistent/assistant.js")
        with mock.patch.dict(embedded_gateway.RUNTIME_MODULES,
                             {"/v1/runtime/assistant.js": missing}):
            status, _, body = self.request("GET", "/v1/runtime/assistant.js")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["code"], "runtime_unavailable")

    def test_liveness_and_default_readiness_are_content_free(self):
        status, _, body = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {
            "ok": True, "protocol": 1, "schema_version": 1})
        status, _, body = self.request("GET", "/readyz")
        self.assertEqual(status, 200)
        readiness = json.loads(body)
        self.assertTrue(readiness["ready"])
        self.assertEqual(readiness["session_count"], 1)
        self.assertNotIn(self.grant.session_id, body.decode())
        self.assertNotIn(self.grant.client_token, body.decode())

    def test_readiness_callback_failure_is_masked_and_fails_closed(self):
        def broken_status():
            raise RuntimeError("secret readiness detail")

        with EmbeddedGateway(
                RemoteSessionRegistry(), sse_wait_s=0.01,
                readiness_provider=broken_status) as gateway:
            request = urllib.request.Request(gateway.base_url + "/readyz")
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=2)
            self.assertEqual(raised.exception.code, 503)
            body = raised.exception.read()
        readiness = json.loads(body)
        self.assertFalse(readiness["ready"])
        self.assertTrue(readiness["status_error"])
        self.assertNotIn(b"secret", body)

    def test_open_provider_circuit_makes_readiness_unavailable(self):
        def status():
            return {
                "ready": True,
                "accepting_sessions": True,
                "session_count": 2,
                "session_capacity": 10,
                "provider": {
                    "max_concurrent_requests": 4,
                    "in_flight": 0,
                    "consecutive_transient_failures": 3,
                    "circuit_open": True,
                    "retry_after_s": 12.5,
                    "untrusted_extra": "must not be exposed",
                },
                "untrusted_extra": "must not be exposed",
            }

        with EmbeddedGateway(
                RemoteSessionRegistry(), sse_wait_s=0.01,
                readiness_provider=status) as gateway:
            request = urllib.request.Request(gateway.base_url + "/readyz")
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=2)
            self.assertEqual(raised.exception.code, 503)
            body = raised.exception.read()
        readiness = json.loads(body)
        self.assertFalse(readiness["ready"])
        self.assertFalse(readiness["accepting_sessions"])
        self.assertTrue(readiness["provider"]["circuit_open"])
        self.assertEqual(readiness["provider"]["retry_after_s"], 12.5)
        self.assertNotIn(b"untrusted_extra", body)

    def test_gateway_builds_one_ephemeral_first_party_bootstrap(self):
        source = self.gateway.bootstrap_script(
            self.grant, page_controller_module=ORIGIN + "/page-controller.js")
        self.assertIn(self.gateway.runtime_url, source)
        self.assertIn(self.grant.session_id, source)
        # The harness hands the page a whole session and loads the same `assistant.js` a
        # production page includes, so both are booted by the same supervisor.
        self.assertIn("window.__WOA_EMBED_CONFIG__ =", source)
        self.assertIn("__WOA_ASSISTANT__", source)
        self.assertNotIn("startEmbeddedRuntime", source)
        self.assertIn("location.origin", source)
        self.assertIn(ORIGIN, source)
        self.assertNotIn("ControlPlan", source)
        with self.assertRaisesRegex(ValueError, "allowed session origin"):
            self.gateway.bootstrap_script(
                self.grant, page_controller_module="http://other.test/controller.js")

    def test_public_proxy_url_is_separate_from_the_loopback_listener(self):
        registry = RemoteSessionRegistry()
        grant, _channel = registry.create(
            allowed_origins=["https://app.example"], session_id="proxied-session")
        with EmbeddedGateway(
                registry, public_base_url="https://app.example/assistant") as gateway:
            config = gateway.bootstrap_config(
                grant,
                page_controller_module="https://app.example/woa/page-controller.js")
            self.assertTrue(gateway.base_url.startswith("http://127.0.0.1:"))
            self.assertEqual(gateway.public_base_url, "https://app.example/assistant")
            self.assertEqual(config["endpoint"], "https://app.example/assistant")
            self.assertEqual(
                gateway.runtime_url,
                "https://app.example/assistant/v1/runtime/assistant.js")
            source = gateway.bootstrap_script(
                grant,
                page_controller_module="https://app.example/woa/page-controller.js")
            self.assertIn("https://app.example/assistant/v1/runtime/assistant.js", source)
            self.assertNotIn(gateway.base_url, source)

    def test_non_loopback_bind_requires_an_explicit_private_network_boundary(self):
        with self.assertRaisesRegex(ValueError, "loopback"):
            EmbeddedGateway(host="0.0.0.0")
        with EmbeddedGateway(
                host="0.0.0.0", allow_private_network_bind=True) as gateway:
            self.assertTrue(gateway.base_url.startswith("http://127.0.0.1:"))

    def test_public_proxy_url_refuses_downgrade_and_ambiguous_paths(self):
        for value in (
                "http://app.example/assistant",
                "https://user:secret@app.example/assistant",
                "https://app.example/assistant/",
                "https://app.example/assistant?token=secret",
                "https://app.example/a/../assistant"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "public_base_url"):
                    EmbeddedGateway(public_base_url=value)

    def test_hello_page_origin_must_match_browser_origin_header(self):
        registry = RemoteSessionRegistry()
        grant, _ = registry.create(
            allowed_origins=[ORIGIN, "http://other.test"],
            session_id="multi-origin-session",
        )
        with EmbeddedGateway(registry, sse_wait_s=0.01) as gateway:
            body = hello(grant.session_id).to_json()
            body["page_origin"] = "http://other.test"
            payload = json.dumps(body).encode()
            request = urllib.request.Request(
                gateway.base_url + f"/v1/sessions/{grant.session_id}/hello",
                data=payload,
                method="POST",
                headers={"Origin": ORIGIN,
                         "Authorization": "Bearer " + grant.client_token,
                         "Content-Type": "application/json"},
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=2)
            self.assertEqual(raised.exception.code, 403)
            self.assertEqual(json.loads(raised.exception.read())["code"],
                             "origin_refused")

    def test_privacy_boundary_rejects_session_and_generic_credentials(self):
        sid = self.grant.session_id
        _, _, raw = self.request("POST", f"/v1/sessions/{sid}/hello",
                                 hello(sid).to_json())
        self.lease = json.loads(raw)["lease_id"]
        handoff = observation(
            sid, 1, url=ORIGIN + "/?externalLoginKey=secret-value")
        raw_token = observation(
            sid, 1, url=ORIGIN + "/callback?access_token=raw-secret&next=1")
        encoded_token = observation(
            sid, 1,
            url=ORIGIN + "/callback?access_token%253Dencoded-secret%2526next%253D1")
        triple_encoded_key_token = observation(
            sid, 1, url=(ORIGIN +
                "/callback?access%25255Ftoken%25253Dtriple-secret%252526next%25253D1"))
        triple_encoded_handoff = observation(
            sid, 1, url=(ORIGIN +
                "/callback?external%25254CoginKey%25253Dhandoff-secret%252526next%25253D1"))
        structured = observation(sid, 1)
        structured.observation.elements[0].attrs.update(
            {"name": "access_token", "value": "structured-secret"})
        direct = ObservationEnvelope(
            schema_version=SCHEMA_VERSIONS["ObservationEnvelope"],
            session_id=sid, observation_id="o-direct", page_epoch=1,
            observation=observation(sid, 1).observation,
            events=({"api_key": "direct-secret"},),
        )
        for leaked in (handoff, raw_token, encoded_token, triple_encoded_key_token,
                       triple_encoded_handoff, structured, direct):
            with self.subTest(leaked=leaked.observation_id):
                status, _, body = self.request(
                    "POST", f"/v1/sessions/{sid}/observations", leaked.to_json())
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body)["code"], "privacy_boundary")

        redacted = observation(
            sid, 1,
            url=ORIGIN + "/callback?access_token%253D%255Bredacted%255D")
        redacted.observation.elements[0].attrs.update(
            {"name": "access_token", "value": "[redacted]"})
        self.assertEqual(self.request(
            "POST", f"/v1/sessions/{sid}/observations", redacted.to_json())[0], 200)

    def test_session_endpoints_require_current_runtime_lease(self):
        sid = self.grant.session_id
        _, _, raw = self.request("POST", f"/v1/sessions/{sid}/hello",
                                 hello(sid).to_json())
        first_lease = json.loads(raw)["lease_id"]
        self.lease = first_lease
        first = observation(sid, 1)

        status, _, body = self.request(
            "POST", f"/v1/sessions/{sid}/observations", first.to_json(), lease="")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["code"], "invalid_lease")
        status, _, body = self.request(
            "POST", f"/v1/sessions/{sid}/observations", first.to_json(),
            lease="fabricated-lease")
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["code"], "runtime_replaced")
        self.assertEqual(self.request(
            "POST", f"/v1/sessions/{sid}/observations", first.to_json())[0], 200)

        _, _, takeover_raw = self.request(
            "POST", f"/v1/sessions/{sid}/hello",
            hello(sid, runtime_instance_id="runtime-document-2").to_json(), lease="")
        self.lease = json.loads(takeover_raw)["lease_id"]
        status, _, body = self.request(
            "GET", f"/v1/sessions/{sid}/events", lease=first_lease)
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["code"], "runtime_replaced")

    def test_cors_preflight_advertises_lease_header(self):
        sid = self.grant.session_id
        status, headers, _ = self.request(
            "OPTIONS", f"/v1/sessions/{sid}/observations")
        self.assertEqual(status, 204)
        self.assertIn("X-WOA-Lease", headers["Access-Control-Allow-Headers"])

    def test_bootstrap_requires_loadable_page_controller_url(self):
        with self.assertRaises(TypeError):
            self.gateway.bootstrap_config(self.grant)
        for invalid in ("@page-agent/page-controller", "/assets/page-controller.js",
                        "javascript:alert(1)", "https://user:pass@example.test/module.js"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "absolute"):
                self.gateway.bootstrap_config(
                    self.grant, page_controller_module=invalid)
        with self.assertRaisesRegex(ValueError, "allowed session origin"):
            self.gateway.bootstrap_config(
                self.grant,
                page_controller_module="https://cdn.example.test/page-controller.js?v=1",
            )
        module_url = ORIGIN + "/page-controller.js?v=1"
        config = self.gateway.bootstrap_config(
            self.grant, page_controller_module=module_url,
            panel_labels={"assistant": "Operations copilot", "send": "Send",
                          "statusReady": "Connected"},
            sensitive_query_keys={"handoffKey"})
        self.assertEqual(config["pageControllerModule"], module_url)
        self.assertEqual(config["navigationMode"], "document")
        self.assertEqual(config["principalExpiresAt"], 0)
        self.assertEqual(config["sessionExpiresAt"], self.grant.session_expires_at)
        self.assertEqual(config["panelLabels"], {
            "assistant": "Operations copilot", "send": "Send",
            "statusReady": "Connected"})
        self.assertEqual(config["sensitiveQueryKeys"], ["handoffkey"])
        with self.assertRaisesRegex(ValueError, "unknown panel label"):
            self.gateway.bootstrap_config(
                self.grant, page_controller_module=module_url,
                panel_labels={"madeUp": "No"})
        with self.assertRaisesRegex(ValueError, "1-200"):
            self.gateway.bootstrap_config(
                self.grant, page_controller_module=module_url,
                panel_labels={"assistant": "bad\nlabel"})
        spa = self.gateway.bootstrap_config(
            self.grant, page_controller_module=module_url, navigation_mode="spa")
        self.assertEqual(spa["navigationMode"], "spa")
        with self.assertRaisesRegex(ValueError, "navigation_mode"):
            self.gateway.bootstrap_config(
                self.grant, page_controller_module=module_url,
                navigation_mode="invented")
        with self.assertRaisesRegex(ValueError, "sensitive_query_keys"):
            self.gateway.bootstrap_config(
                self.grant, page_controller_module=module_url,
                sensitive_query_keys={"bad key"})
        normalized = self.gateway.bootstrap_config(
            self.grant,
            page_controller_module="http://APP.TEST:80/page-controller.js",
        )
        self.assertEqual(normalized["pageControllerModule"],
                         "http://APP.TEST:80/page-controller.js")


if __name__ == "__main__":
    unittest.main()
