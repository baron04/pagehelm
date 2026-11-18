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
from web_operation_agent.state.identity import OperationContext, PrincipalContext
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

from web_operation_agent.tests.transport.support import (
    DEFAULT_LEASE, ORIGIN, action_for, completed, hello, observation, principal,
)


class ChannelTests(unittest.TestCase):
    def test_complete_write_details_require_the_active_browser_capability(self):
        from web_operation_agent.schemas.approval_details import ApprovalDetail
        request = ApprovalRequest(1, "long-write", "Publish synthetic note", ("Synthetic team",),
            int(time.time()*1000)+30000, details=(ApprovalDetail("body", "Body", "complete body"),))
        with self.assertRaises(RemoteProtocolError) as caught:
            self.channel.request_approval(request)
        self.assertEqual(caught.exception.code, "approval_details_unsupported")
        self.assertFalse(self.channel._approvals)
        advertised = dataclasses.replace(hello(self.grant.session_id, runtime_instance_id="upgraded-document"),
            capabilities=hello(self.grant.session_id).capabilities + ("approval.details.v1",))
        self.channel.accept_hello(advertised)
        event = self.channel.request_approval(request)
        self.assertEqual(event.payload["details"][0]["text"], "complete body")

    def test_read_context_uses_the_active_turn_document_and_rejects_foreign_bindings(self):
        from web_operation_agent.state.identity import PrincipalError
        now = int(time.time()*1000)
        actor = principal(now)
        grant, channel = RemoteSessionRegistry().create(allowed_origins=[ORIGIN], principal=actor)
        leases = []
        for number in (1,2):
            lease = channel.accept_hello(hello(grant.session_id,client_id=f"tab-{number}",
                runtime_instance_id=f"doc-{number}",application_id="sample",
                principal_digest=actor.binding_digest))["lease_id"]
            page = dataclasses.replace(observation(grant.session_id,number).observation,
                page_route="/start",page_query={"path":"/api/report", "parameters":{"tenantView":str(number)}})
            channel.accept_observation(ObservationEnvelope(1,grant.session_id,f"read-{number}",number,page),lease_id=lease)
            leases.append(lease)
        channel.accept_message({"message_id":"read-turn","text":"Read this report","page_url":ORIGIN+"/start"},lease_id=leases[0])
        channel.wait_for_message(timeout_s=0)
        context = OperationContext.create(actor,session_id=grant.session_id,turn_id="read-turn",
                                         operation_id="read",deadline_ms=now+30000)
        self.assertEqual("1",channel.page_for_read(context).observation.page_query["parameters"]["tenantView"])
        for c in (None,dataclasses.replace(context,session_id="other-session"),
                  dataclasses.replace(context,turn_id="other-turn"),
                  dataclasses.replace(context,principal=principal(now,actor_id="other"))):
            with self.assertRaises(PrincipalError):channel.page_for_read(c)
        channel._release_lease(leases[0])
        with self.assertRaises(RemoteProtocolError):channel.page_for_read(context)
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

    def test_execution_diagnostic_changes_do_not_conflict_with_an_acknowledged_result(self):
        action = action_for(self.grant.session_id, self.first)
        self.channel.enqueue_action(action)
        self.channel.claim_action({"action_id":action.action_id}, lease_id=self.lease)
        after = observation(self.grant.session_id,2,caused_by=action.action_id,url=ORIGIN + "/next")
        result = completed(action,after)
        trace = {"trace_id":"trace-original","events":[{"sequence":1,"operation":"click","path":"ordinary"}]}
        traced = dataclasses.replace(result,receipt=dataclasses.replace(result.receipt,execution_trace=trace))
        self.assertFalse(self.channel.accept_result(traced,lease_id=self.lease)["duplicate"])
        delivered = self.channel.wait_for_result(action.action_id,timeout_s=0)
        self.assertEqual(delivered.receipt.execution_trace,trace)
        changed = dataclasses.replace(result,receipt=dataclasses.replace(result.receipt,execution_trace={
            "trace_id":"trace-replayed","events":[{"sequence":1,"operation":"click","path":"binding"}]}))
        self.assertTrue(self.channel.accept_result(changed,lease_id=self.lease)["duplicate"])

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

    def test_the_panel_names_a_control_from_the_page_the_action_was_based_on(self):
        action = action_for(self.grant.session_id, self.first)
        self.channel.enqueue_action(action)

        def step() -> str:
            entries = self.channel.panel_timeline(lease_id=self.lease)["entries"]
            return next(entry["text"] for entry in entries if entry["kind"] == "action")

        self.assertEqual(step(), "点击“Next”链接")
        # The result replaces the page; the step still reads as it did when it was taken.
        after = observation(self.grant.session_id, 2, caused_by=action.action_id,
                            url=ORIGIN + "/next")
        self.channel.claim_action({"action_id": action.action_id}, lease_id=self.lease)
        self.channel.accept_result(completed(action, after), lease_id=self.lease)
        self.assertEqual(step(), "点击“Next”链接")

    def test_page_facet_action_requires_an_advertised_runtime_capability(self):
        def facet_action(session_id, current):
            command = ActionCommand("a-facet", "apply_facet",
                                    {"ref": "fc_1", "active": True})
            return dataclasses.replace(
                action_for(session_id, current, action_id="a-facet"),
                command=command, command_digest=command.content_digest)

        with self.assertRaisesRegex(RemoteProtocolError, "page.apply_facet"):
            self.channel.enqueue_action(facet_action(self.grant.session_id, self.first))

        registry = RemoteSessionRegistry()
        grant, channel = registry.create(allowed_origins=[ORIGIN], session_id="facet-session")
        advertised = hello(grant.session_id)
        reply = channel.accept_hello(dataclasses.replace(
            advertised, capabilities=advertised.capabilities + ("page.apply_facet",)))
        current = observation(grant.session_id, 1)
        channel.accept_observation(current, lease_id=reply["lease_id"])
        self.assertEqual(channel.enqueue_action(facet_action(grant.session_id, current)).kind,
                         "action")

    def test_form_handoff_requires_an_advertised_runtime_capability(self):
        command = ActionCommand("a-form-handoff", "handoff_form", {"fields": [{
            "ref": "e1", "field": "name", "control": "textbox", "value": "new"}]})
        action = dataclasses.replace(
            action_for(self.grant.session_id, self.first, action_id="a-form-handoff"),
            command=command, command_digest=command.content_digest)
        with self.assertRaisesRegex(RemoteProtocolError, "page.handoff_form"):
            self.channel.enqueue_action(action)

        registry = RemoteSessionRegistry()
        grant, channel = registry.create(allowed_origins=[ORIGIN], session_id="handoff-session")
        advertised = hello(grant.session_id)
        reply = channel.accept_hello(dataclasses.replace(
            advertised, capabilities=advertised.capabilities + ("page.handoff_form",)))
        current = observation(grant.session_id, 1)
        channel.accept_observation(current, lease_id=reply["lease_id"])
        issued = dataclasses.replace(action, session_id=grant.session_id,
                                     based_on_observation_id=current.observation_id,
                                     page_epoch=current.page_epoch)
        self.assertEqual(channel.enqueue_action(issued).kind, "action")

    def test_scoped_handoff_cannot_be_silently_accepted_by_an_older_runtime(self):
        from web_operation_agent.schemas.browser import SCOPED_FORM_HANDOFF_CAPABILITY
        for args in (
            {"fields": [{"ref": "e1", "field": "name", "value": "new",
                         "control": "textbox", "scope": "form:editor"}]},
            {"fields": [{"ref": "e1", "field": "name", "value": "new",
                         "control": "textbox", "binding": "label", "label": "Name"}]},
            {"fields": [], "submit": {"ref": "e2", "label": "Save", "scope": "form:editor"}},
        ):
            with self.subTest(args=args):
                for supported in (False, True):
                    registry = RemoteSessionRegistry()
                    grant, channel = registry.create(allowed_origins=[ORIGIN])
                    advertised = hello(grant.session_id)
                    caps = advertised.capabilities + ("page.handoff_form",)
                    if supported:
                        caps += (SCOPED_FORM_HANDOFF_CAPABILITY,)
                    reply = channel.accept_hello(dataclasses.replace(advertised, capabilities=caps))
                    current = observation(grant.session_id, 1)
                    channel.accept_observation(current, lease_id=reply["lease_id"])
                    command = ActionCommand("scoped", "handoff_form", args)
                    action = dataclasses.replace(action_for(grant.session_id, current, action_id="scoped"),
                        command=command, command_digest=command.content_digest)
                    if supported:
                        self.assertEqual(channel.enqueue_action(action).kind, "action")
                    else:
                        with self.assertRaisesRegex(RemoteProtocolError, SCOPED_FORM_HANDOFF_CAPABILITY):
                            channel.enqueue_action(action)

    def test_principal_bound_session_requires_exact_runtime_binding(self):
        now = int(time.time() * 1000)
        expected = principal(now)
        registry = RemoteSessionRegistry()
        grant, channel = registry.create(
            allowed_origins=[ORIGIN], session_id="principal-session",
            principal=expected)

        self.assertIs(channel.principal, expected)
        self.assertEqual(grant.application_id, "sample")
        self.assertEqual(grant.principal_digest, expected.binding_digest)
        self.assertEqual(grant.expires_at, expected.expires_at)
        self.assertNotIn(expected.actor_id, repr(grant))
        with self.assertRaisesRegex(RemoteProtocolError, "principal binding") as missing:
            channel.accept_hello(hello(grant.session_id))
        self.assertEqual(missing.exception.code, "principal_mismatch")
        with self.assertRaisesRegex(RemoteProtocolError, "principal binding"):
            channel.accept_hello(hello(
                grant.session_id, application_id="sample",
                principal_digest="0" * 64))
        reply = channel.accept_hello(hello(
            grant.session_id, application_id="sample",
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

    def test_a_renewal_equal_to_the_current_principal_is_a_no_op_not_a_refusal(self):
        initial = principal(1_000)
        with mock.patch("web_operation_agent.transport.channel._now_ms", return_value=2_000), \
             mock.patch("web_operation_agent.transport.registry._now_ms", return_value=2_000):
            registry = RemoteSessionRegistry(limits=RemoteSessionLimits(session_ttl_s=300))
            _grant, channel = registry.create(
                allowed_origins=[ORIGIN], session_id="equal-renewal", principal=initial)
            # Two of a person's pages can bootstrap in the same millisecond: an
            # identical grant is keep-existing, never a sign-in refusal.
            channel.renew_principal(dataclasses.replace(initial, expires_at=initial.expires_at))
            self.assertEqual(channel.principal.expires_at, initial.expires_at)
            with self.assertRaisesRegex(Exception, "shortens"):
                channel.renew_principal(dataclasses.replace(
                    initial, expires_at=initial.expires_at - 1))

    def test_principal_renewal_refuses_an_active_turn(self):
        now = int(time.time() * 1000)
        initial = principal(now)
        registry = RemoteSessionRegistry()
        grant, channel = registry.create(
            allowed_origins=[ORIGIN], session_id="busy-renewal", principal=initial)
        lease = channel.accept_hello(hello(
            grant.session_id, application_id="sample",
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
                application_id="sample", principal_digest="0" * 64))
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
            grant.session_id, application_id="sample",
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
            grant.session_id, application_id="sample",
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

    def test_prefill_choice_requires_an_explicit_write_offer_and_is_single_use(self):
        expires = int(time.time() * 1000) + 5000
        plain = ApprovalRequest(1, "plain-write", "Update record", ("Record 1",), expires)
        self.channel.request_approval(plain)
        unoffered = ApprovalDecision(1, self.grant.session_id, plain.approval_id, "prefill")
        with self.assertRaises(RemoteProtocolError) as caught:
            self.channel.accept_approval(unoffered, lease_id=self.lease)
        self.assertEqual(caught.exception.code, "approval_choice_unavailable")
        self.channel.accept_approval(dataclasses.replace(unoffered, decision="denied"),
                                     lease_id=self.lease)

        offered = ApprovalRequest(1, "offered-write", "Update record", ("Record 1",),
                                  expires, offer_prefill=True)
        self.channel.request_approval(offered)
        prefill = dataclasses.replace(unoffered, approval_id=offered.approval_id)
        self.assertFalse(self.channel.accept_approval(prefill, lease_id=self.lease)["duplicate"])
        self.assertTrue(self.channel.accept_approval(prefill, lease_id=self.lease)["duplicate"])
        with self.assertRaises(RemoteProtocolError) as conflict:
            self.channel.accept_approval(dataclasses.replace(prefill, decision="approved"),
                                         lease_id=self.lease)
        self.assertEqual(conflict.exception.code, "approval_conflict")

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

    def test_cancellation_requires_current_lease_valid_ids_and_same_session(self):
        message = {"message_id": "message-security", "text": "Open next",
                   "page_url": ORIGIN + "/start"}
        self.channel.accept_message(message, lease_id=self.lease)
        pre = self.channel.accept_cancellation(
            CancellationRequest(1, self.grant.session_id, "missing"),
            lease_id=self.lease)
        self.assertEqual(pre, {"accepted": True, "duplicate": False, "pre_cancelled": True})
        self.assertEqual(
            self.channel.accept_cancellation(
                CancellationRequest(1, self.grant.session_id, "missing"),
                lease_id=self.lease),
            {"accepted": True, "duplicate": True, "pre_cancelled": True},
        )
        for bad_id in ("bad id", "x" * 129):
            with self.assertRaises(RemoteProtocolError) as invalid:
                self.channel.accept_cancellation(
                    CancellationRequest(1, self.grant.session_id, bad_id),
                    lease_id=self.lease)
            self.assertEqual(invalid.exception.status, 400)
        with self.assertRaisesRegex(RemoteProtocolError, "another session"):
            self.channel.accept_cancellation(
                CancellationRequest(1, "other-session", "message-security"),
                lease_id=self.lease)
        new_lease = self.channel.accept_hello(hello(
            self.grant.session_id, runtime_instance_id="runtime-document-2", client_id="browser-tab-2"))["lease_id"]
        with self.assertRaisesRegex(RemoteProtocolError, "lease"):
            self.channel.accept_cancellation(
                CancellationRequest(1, self.grant.session_id, "message-security"),
                lease_id="fabricated-lease")
        # Another page of the same session may stop the task.
        self.assertTrue(self.channel.accept_cancellation(
            CancellationRequest(1, self.grant.session_id, "message-security"),
            lease_id=new_lease)["accepted"])

    def test_pre_cancellation_handles_early_stop_race_and_duplicate_message(self):
        pre = self.channel.accept_cancellation(
            CancellationRequest(1, self.grant.session_id, "raced-msg"),
            lease_id=self.lease)
        self.assertEqual(pre, {"accepted": True, "duplicate": False, "pre_cancelled": True})
        msg = {"message_id": "raced-msg", "text": "Late arrival", "page_url": ORIGIN + "/start"}
        reply = self.channel.accept_message(msg, lease_id=self.lease)
        self.assertEqual(reply, {"accepted": True, "duplicate": False, "cancelled": True})
        self.assertTrue(self.channel.message_cancelled("raced-msg"))
        with self.assertRaises(TimeoutError):
            self.channel.wait_for_message(timeout_s=0.05)
        states = [event.payload.get("state") for event in
                  self.channel.events_after("", lease_id=self.lease)
                  if event.kind == "progress" and event.turn_id == "raced-msg"]
        self.assertEqual(states, ["cancelled"])
        self.assertEqual(
            self.channel.accept_message(msg, lease_id=self.lease),
            {"accepted": True, "duplicate": True},
        )

    def test_pre_cancellation_evicts_oldest_when_at_capacity_and_expires_by_ttl(self):
        with mock.patch("web_operation_agent.transport.channel._now_ms", return_value=1_000):
            for idx in range(65):
                reply = self.channel.accept_cancellation(
                    CancellationRequest(1, self.grant.session_id, f"cap-{idx}"),
                    lease_id=self.lease)
                self.assertTrue(reply["pre_cancelled"])
            # cap-0 was evicted; cap-64 remains pre-cancelled.
            evicted = self.channel.accept_message(
                {"message_id": "cap-0", "text": "Run evicted", "page_url": ORIGIN + "/start"},
                lease_id=self.lease)
            self.assertEqual(evicted, {"accepted": True, "duplicate": False})
            self.assertEqual(self.channel.wait_for_message(timeout_s=0)["message_id"], "cap-0")
            self.channel.complete_message("cap-0")
            still_cancelled = self.channel.accept_message(
                {"message_id": "cap-64", "text": "Cancelled", "page_url": ORIGIN + "/start"},
                lease_id=self.lease)
            self.assertEqual(still_cancelled, {"accepted": True, "duplicate": False, "cancelled": True})

            self.channel.accept_cancellation(
                CancellationRequest(1, self.grant.session_id, "ttl-msg"),
                lease_id=self.lease)
        # After 120s TTL expires, late message is queued normally.
        with mock.patch("web_operation_agent.transport.channel._now_ms", return_value=121_000):
            after_ttl = self.channel.accept_message(
                {"message_id": "ttl-msg", "text": "After TTL", "page_url": ORIGIN + "/start"},
                lease_id=self.lease)
            self.assertEqual(after_ttl, {"accepted": True, "duplicate": False})
            self.assertEqual(self.channel.wait_for_message(timeout_s=0)["message_id"], "ttl-msg")

    def test_transient_sqlite_errors_do_not_close_healthy_durable_channel(self):
        import sqlite3
        import tempfile
        from pathlib import Path
        from web_operation_agent.state import SQLiteSessionStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteSessionStore(Path(tmp) / "sessions.sqlite3")
            registry = RemoteSessionRegistry(durable_store=store)
            actor = principal(int(time.time() * 1000))
            grant, channel = registry.create(
                allowed_origins=[ORIGIN], session_id="sqlite-transient", principal=actor)
            try:
                lease = channel.accept_hello(hello(
                    grant.session_id, application_id="sample",
                    principal_digest=actor.binding_digest))["lease_id"]
                with mock.patch.object(
                        store, "message",
                        side_effect=sqlite3.OperationalError("database is locked")):
                    with self.assertRaises(RemoteProtocolError) as caught:
                        channel.accept_message(
                            {"message_id": "m-retry", "text": "Retry me",
                             "page_url": ORIGIN + "/start"},
                            lease_id=lease)
                    self.assertEqual(caught.exception.status, 503)
                    self.assertEqual(caught.exception.code, "gateway_error")
                channel.validate_live()

                # Succeeds once contention clears.
                self.assertEqual(
                    channel.accept_message(
                        {"message_id": "m-retry", "text": "Retry me",
                         "page_url": ORIGIN + "/start"},
                        lease_id=lease),
                    {"accepted": True, "duplicate": False},
                )
                with mock.patch.object(
                        store, "transition",
                        side_effect=sqlite3.OperationalError("database is locked")):
                    channel.accept_cancellation(
                        CancellationRequest(1, grant.session_id, "m-retry"),
                        lease_id=lease)
                    channel.complete_message("m-retry")
                channel.validate_live()

                # Renew contention returns recoverable 503 without closing session.
                with mock.patch.object(
                        store, "renew",
                        side_effect=sqlite3.OperationalError("database is locked")):
                    with self.assertRaises(RemoteProtocolError) as renew_err:
                        channel.accept_message(
                            {"message_id": "m-next", "text": "Next",
                             "page_url": ORIGIN + "/start"},
                            lease_id=lease)
                    self.assertEqual(renew_err.exception.status, 503)
                channel.validate_live()

                # Non-transient DatabaseError (corruption) still fails closed.
                with mock.patch.object(
                        store, "transition",
                        side_effect=sqlite3.DatabaseError("database disk image is malformed")):
                    channel.complete_message("m-next")
                with self.assertRaises(RemoteSessionClosed):
                    channel.validate_live()
            finally:
                channel.close()

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

    def test_action_deadline_and_wait_use_the_remaining_turn_budget(self):
        browser = RemoteBrowserClient(self.channel, timeout_s=2)
        browser.start()
        before_ms = int(time.time() * 1000)
        with mock.patch.object(self.channel, "wait_for_result", side_effect=TimeoutError) as wait:
            with self.assertRaises(TransportError):
                browser.execute_with_budget("fill", {"ref": "e0", "value": "Draft"},
                                            timeout_s=0.25)
        envelope = self.channel.pending_action
        self.assertIsNotNone(envelope)
        self.assertGreaterEqual(envelope.deadline, before_ms + 250)
        self.assertLessEqual(envelope.deadline, int(time.time() * 1000) + 250)
        self.assertEqual(wait.call_args.kwargs["timeout_s"], 0.25)
        self.assertEqual(browser.timeout_s, 2)
        self.assertEqual(browser.command_count, 1)

    def test_turn_budget_cannot_extend_the_configured_action_timeout(self):
        browser = RemoteBrowserClient(self.channel, timeout_s=0.5)
        browser.start()
        with mock.patch.object(self.channel, "wait_for_result", side_effect=TimeoutError) as wait:
            with self.assertRaises(TransportError):
                browser.execute_with_budget("observe", {}, timeout_s=10)
        self.assertEqual(wait.call_args.kwargs["timeout_s"], 0.5)

    def test_invalid_action_budgets_never_enqueue(self):
        browser = RemoteBrowserClient(self.channel, timeout_s=2)
        browser.start()
        for invalid in (0, -1, float("nan"), float("inf"), True, "1"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                browser.execute_with_budget("fill", {"ref": "e0", "value": "Draft"},
                                            timeout_s=invalid)
        self.assertEqual(browser.command_count, 0)
        self.assertIsNone(self.channel.pending_action)

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

    def test_full_navigation_replaces_only_its_tab_and_finishes_without_redispatch(self):
        action = action_for(self.grant.session_id, self.first)
        self.channel.enqueue_action(action)
        self.channel.claim_action({"action_id": action.action_id}, lease_id=self.lease)
        other = self.channel.accept_hello(hello(
            self.grant.session_id, runtime_instance_id="other-tab", client_id="other-tab"))["lease_id"]
        fresh = self.channel.accept_hello(hello(
            self.grant.session_id, runtime_instance_id="new-document"))["lease_id"]
        self.channel.validate_lease(other)
        with self.assertRaises(RemoteProtocolError):
            self.channel.validate_lease(self.lease)
        with self.assertRaises(RemoteProtocolError):
            self.channel.accept_hello(hello(self.grant.session_id))
        self.assertEqual(self.channel.claim_action({"action_id": action.action_id}, lease_id=fresh),
                         {"claimed": False, "reason": "already_claimed"})
        after = observation(self.grant.session_id, 2, caused_by=action.action_id,
                            url=ORIGIN + "/next")
        self.channel.accept_result(completed(action, after), lease_id=fresh)
        self.assertEqual(self.channel.latest_observation_envelope, after)
        self.assertNotIn("action", [e.kind for e in self.channel.events_after(lease_id=other)])

    def test_navigation_preserves_queued_messages_for_its_tab(self):
        self.channel.accept_message({"message_id": "queued-navigation", "text": "one",
                                     "page_url": ORIGIN + "/start"}, lease_id=self.lease)
        fresh = self.channel.accept_hello(hello(
            self.grant.session_id, runtime_instance_id="new-document"))["lease_id"]
        self.channel.accept_hello(hello(
            self.grant.session_id, runtime_instance_id="other-tab", client_id="other-tab"))
        self.channel.wait_for_message(timeout_s=0)
        self.assertEqual(self.channel._active_lease_id, fresh)

    def test_a_second_page_joins_without_taking_the_first_pages_action(self):
        action = action_for(self.grant.session_id, self.first)
        self.channel.enqueue_action(action)
        self.assertFalse(self.channel.claim_action(
            {"action_id": action.action_id}, lease_id=self.lease)["duplicate"])

        second = self.channel.accept_hello(hello(
            self.grant.session_id, runtime_instance_id="runtime-document-2", client_id="browser-tab-2"))["lease_id"]
        self.assertNotEqual(second, self.lease)
        # Both pages stay open on the session.
        self.channel.validate_lease(self.lease)
        self.channel.validate_lease(second)
        # The action is the first page's: the second is neither handed it nor may claim it.
        self.assertNotIn("action", [event.kind for event in
                                    self.channel.events_after(lease_id=second)])
        self.assertIn("action", [event.kind for event in
                                 self.channel.events_after(lease_id=self.lease)])
        self.assertEqual(self.channel.claim_action(
            {"action_id": action.action_id}, lease_id=second),
            {"claimed": False, "reason": "another_page"})
        after = observation(self.grant.session_id, 2, caused_by=action.action_id,
                            url=ORIGIN + "/next")
        with self.assertRaisesRegex(RemoteProtocolError, "current task"):
            self.channel.accept_result(completed(action, after), lease_id=second)
        self.assertFalse(self.channel.accept_result(
            completed(action, after), lease_id=self.lease)["duplicate"])

    def test_a_turn_runs_on_the_page_that_sent_it(self):
        second = self.channel.accept_hello(hello(
            self.grant.session_id, runtime_instance_id="runtime-document-2", client_id="browser-tab-2"))["lease_id"]
        there = dataclasses.replace(
            observation(self.grant.session_id, 1, url=ORIGIN + "/other"),
            observation_id="o-second-1")
        self.channel.accept_observation(there, lease_id=second)
        # The first page is still the one observed until a turn from the second begins.
        self.assertEqual(self.channel.latest_observation_envelope.observation_id, "o-1")
        self.channel.accept_message({"message_id": "m-first", "text": "one",
                                     "page_url": ORIGIN + "/start"}, lease_id=self.lease)
        self.channel.accept_message({"message_id": "m-second", "text": "two",
                                     "page_url": ORIGIN + "/other"}, lease_id=second)
        self.assertEqual(self.channel.wait_for_message(timeout_s=1)["message_id"], "m-first")
        self.assertEqual(self.channel.latest_observation_envelope.observation_id, "o-1")
        self.channel.complete_message("m-first")
        self.assertEqual(self.channel.wait_for_message(timeout_s=1)["message_id"], "m-second")
        self.assertEqual(self.channel.latest_observation_envelope.observation_id, "o-second-1")
        self.assertEqual(self.channel.wait_for_client(1).runtime_instance_id,
                         "runtime-document-2")

    def test_the_active_page_names_the_window_its_turn_works_through(self):
        self.assertEqual(self.channel.active_instance, "runtime-document-1")
        second = self.channel.accept_hello(hello(
            self.grant.session_id, runtime_instance_id="runtime-document-2",
            client_id="browser-tab-2"))["lease_id"]
        # Joining the conversation does not take the window; the page whose message
        # runs next does, so the shared session's guard work stays its page's own.
        self.assertEqual(self.channel.active_instance, "runtime-document-1")
        self.channel.accept_message({"message_id": "m-second", "text": "two",
                                     "page_url": ORIGIN + "/other"}, lease_id=second)
        self.channel.wait_for_message(timeout_s=1)
        self.assertEqual(self.channel.active_instance, "runtime-document-2")

    def test_asking_from_another_page_stops_a_turn_waiting_for_confirmation(self):
        self.channel.accept_message({"message_id": "m-first", "text": "one",
                                     "page_url": ORIGIN + "/start"}, lease_id=self.lease)
        self.channel.wait_for_message(timeout_s=1)
        self.channel.request_approval(ApprovalRequest(
            schema_version=1, approval_id="approval-1", effect="Operate this page",
            targets=("Page",), expires_at=int(time.time() * 1000) + 60_000))
        second = self.channel.accept_hello(hello(
            self.grant.session_id, runtime_instance_id="runtime-document-2", client_id="browser-tab-2"))["lease_id"]
        # The confirmation is the first page's to give: the second page is not shown it
        # and cannot answer it.
        self.assertNotIn("approval_required", [
            event.kind for event in self.channel.events_after(lease_id=second)])
        with self.assertRaisesRegex(RemoteProtocolError, "current task"):
            self.channel.accept_approval(ApprovalDecision(
                schema_version=1, session_id=self.grant.session_id,
                approval_id="approval-1", decision="approved"), lease_id=second)
        self.channel.accept_message({"message_id": "m-second", "text": "two",
                                     "page_url": ORIGIN + "/start"}, lease_id=second)
        self.assertTrue(self.channel.message_cancelled("m-first"))
        self.assertEqual(self.channel.wait_for_approval("approval-1", timeout_s=0).decision,
                         "denied")
        self.channel.complete_message("m-first")
        self.assertEqual(self.channel.wait_for_message(timeout_s=1)["message_id"], "m-second")
        # Now the second page's turn runs; the first page's card is still not the second's.
        self.assertNotIn("approval_required", [
            event.kind for event in self.channel.events_after(lease_id=second)])

    def test_a_turn_at_work_is_not_stopped_by_another_page_asking(self):
        self.channel.accept_message({"message_id": "m-first", "text": "one",
                                     "page_url": ORIGIN + "/start"}, lease_id=self.lease)
        self.channel.wait_for_message(timeout_s=1)
        second = self.channel.accept_hello(hello(
            self.grant.session_id, runtime_instance_id="runtime-document-2", client_id="browser-tab-2"))["lease_id"]
        self.channel.accept_message({"message_id": "m-second", "text": "two",
                                     "page_url": ORIGIN + "/start"}, lease_id=second)
        self.assertFalse(self.channel.message_cancelled("m-first"))

    def test_new_conversation_on_another_page_keeps_the_running_turn_and_both_leases(self):
        second = self.channel.accept_hello(hello(
            self.grant.session_id, runtime_instance_id="runtime-document-2", client_id="browser-tab-2"))["lease_id"]
        self.channel.accept_message({"message_id": "m-first", "text": "one",
                                     "page_url": ORIGIN + "/start"}, lease_id=self.lease)
        self.channel.wait_for_message(timeout_s=1)
        with self.assertRaises(RemoteProtocolError) as refused:
            self.channel.request_new_conversation(lease_id=second)
        self.assertEqual((refused.exception.code, refused.exception.status), ("turn_in_flight", 409))
        self.assertFalse(self.channel.message_cancelled("m-first"))
        self.assertFalse(self.channel.take_new_conversation())
        self.channel.validate_lease(self.lease)
        self.channel.validate_lease(second)
        self.channel.complete_message("m-first")
        self.assertEqual(self.channel.request_new_conversation(lease_id=second), {"accepted": True})
        self.assertTrue(self.channel.take_new_conversation())

    def test_a_page_long_gone_makes_room_for_a_new_one(self):
        registry = RemoteSessionRegistry(limits=RemoteSessionLimits(max_runtime_instances=2))
        grant, channel = registry.create(allowed_origins=[ORIGIN], session_id="small")
        first = channel.accept_hello(hello(grant.session_id))["lease_id"]
        second = channel.accept_hello(hello(
            grant.session_id, runtime_instance_id="runtime-document-2", client_id="browser-tab-2"))["lease_id"]
        channel.accept_hello(hello(grant.session_id, runtime_instance_id="runtime-document-3", client_id="browser-tab-3"))
        # The first page is the active one and stays; the idle second one made room.
        channel.validate_lease(first)
        # Registry pressure is not a takeover: the page may hello again for a fresh
        # lease where a replaced document must never come back.
        with self.assertRaises(RemoteProtocolError) as caught:
            channel.validate_lease(second)
        self.assertEqual(caught.exception.code, "lease_evicted")
        returned = channel.accept_hello(hello(
            grant.session_id, runtime_instance_id="runtime-document-2",
            client_id="browser-tab-2"))["lease_id"]
        channel.validate_lease(returned)
        self.assertNotEqual(returned, second)

    def test_a_taken_over_page_stays_replaced(self):
        registry = RemoteSessionRegistry()
        grant, channel = registry.create(allowed_origins=[ORIGIN], session_id="takeover")
        first = channel.accept_hello(hello(grant.session_id))["lease_id"]
        channel.accept_hello(hello(
            grant.session_id, runtime_instance_id="runtime-document-2"))
        with self.assertRaises(RemoteProtocolError) as caught:
            channel.validate_lease(first)
        self.assertEqual(caught.exception.code, "runtime_replaced")

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
