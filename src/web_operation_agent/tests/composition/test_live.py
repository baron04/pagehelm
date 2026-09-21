from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from web_operation_agent.composition.live import (LiveConversationRunner, LiveProductServer,
                                      LiveProductSession, render_live_replay)
from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.state.identity import PrincipalContext
from web_operation_agent.modeling.client import ProviderError
from web_operation_agent.execution.product import (AgentTurnConfig, AgentTurnError,
                                         AgentTurnErrorCode, AgentTurnExecutor,
                                         AgentTurnStatus)
from web_operation_agent.transport import (RemoteProtocolError, RemoteSessionClosed,
                                         RemoteSessionLimits, RemoteSessionRegistry)
from web_operation_agent.schemas import (SCHEMA_VERSIONS, CancellationRequest,
                                         ClientHello, ElementRef, ObservationEnvelope,
                                         PageObservation)


ORIGIN = "http://app.test"


class _Runner:
    def __init__(self, value=None, error: BaseException | None = None):
        self.value = value
        self.error = error

    def run_once(self):
        if self.error is not None:
            raise self.error
        return self.value


class _TurnRunner:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error

    def run_once(self, message=None):
        if self.error:
            raise self.error
        return {"message": message["text"], "value": self.value}


@dataclass(frozen=True)
class _TurnResult:
    status: str
    terminal_reason: str


class _RecordingExecutor:
    def __init__(self, channel, *, failures=None, results=None):
        self.channel = channel
        self.failures = failures or {}
        self.results = results or {}
        self.calls = []

    def execute(self, message):
        latest = self.channel.latest_observation_envelope
        assert latest is not None
        self.calls.append({
            "message": message,
            "observation_id": latest.observation_id,
            "page_epoch": latest.page_epoch,
            "page_url": latest.observation.url,
        })
        error = self.failures.get(message["message_id"])
        if error is not None:
            raise error
        return self.results.get(
            message["message_id"], {"message_id": message["message_id"]})


def _message(index: int) -> dict:
    return {
        "message_id": f"message-{index}",
        "text": f"Task {index}",
        "page_url": ORIGIN + "/companies",
    }


def _channel_with_observation():
    registry = RemoteSessionRegistry()
    grant, channel = registry.create(
        allowed_origins=[ORIGIN], session_id="live-conversation-test")
    hello = ClientHello(
        schema_version=SCHEMA_VERSIONS["ClientHello"],
        session_id=grant.session_id,
        client_id="browser-tab-1",
        runtime="test-runtime/1",
        runtime_instance_id="document-1",
        capabilities=("dom.observe", "dom.click", "dom.fill", "dom.select",
                      "dom.submit", "page.goto", "page.back", "page.scroll", "page.wait"),
        page_origin=ORIGIN,
    )
    lease = channel.accept_hello(hello)["lease_id"]
    page = PageObservation(
        schema_version=SCHEMA_VERSIONS["PageObservation"],
        epoch=1,
        url=ORIGIN + "/companies",
        title="Companies",
        landmarks=["Companies"],
        elements=[ElementRef(
            ref="e0", role="button", name="New company", tag="button",
            epoch=1, attrs={})],
        content="[0]<button>New company</button>",
    )
    envelope = ObservationEnvelope(
        schema_version=SCHEMA_VERSIONS["ObservationEnvelope"],
        session_id=grant.session_id,
        observation_id="observation-1",
        page_epoch=1,
        observation=page,
    )
    channel.accept_observation(envelope, lease_id=lease)
    return channel, lease


class LiveProductSessionTests(unittest.TestCase):
    def test_cancel_and_deadline_release_host_for_following_messages(self):
        class Chat:
            model = "live-double"

            def __init__(self):
                self.calls = 0
                self.started = threading.Event()

            def complete(self, *_args, **_kwargs):
                raise AssertionError("cancellable completion was bypassed")

            def complete_cancellable(self, *_args, cancelled, timeout_s, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    self.started.set()
                    while not cancelled():
                        time.sleep(.01)
                    raise ProviderError(
                        "cancelled", code="provider_cancelled", retryable=False)
                if self.calls == 3:
                    time.sleep(timeout_s + .02)
                    raise ProviderError("late", code="provider_transport")
                return {"tool_calls": [{"id": "finish-2", "function": {
                    "name": "finish",
                    "arguments": json.dumps({"done": True, "why": "recovered"}),
                }}]}

        channel, lease = _channel_with_observation()
        chat = Chat()
        with tempfile.TemporaryDirectory() as directory:
            executor = AgentTurnExecutor(
                channel=channel,
                config=AgentTurnConfig(
                    supported_scope="Read the current page.",
                    ledger_path=Path(directory) / "session.jsonl",
                    wall_budget_s=1),
                chat=chat, business_tools=None, control=ReadOnlyControl(),
                run_id_factory=lambda message: "live-" + message.message_id,
            )
            host = LiveConversationRunner(channel, executor=executor)
            worker = threading.Thread(target=host.run, daemon=True)
            worker.start()
            channel.accept_message(_message(1), lease_id=lease)
            self.assertTrue(chat.started.wait(1))
            channel.accept_cancellation(
                CancellationRequest(1, channel.session_id, "message-1"),
                lease_id=lease)
            cancelled = host.next_outcome(timeout_s=1)
            channel.accept_message(_message(2), lease_id=lease)
            recovered = host.next_outcome(timeout_s=1)
            channel.accept_message(_message(3), lease_id=lease)
            timed_out = host.next_outcome(timeout_s=2)
            channel.accept_message(_message(4), lease_id=lease)
            recovered_after_timeout = host.next_outcome(timeout_s=1)
            host.close("test_complete")
            worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(cancelled.result.status, AgentTurnStatus.CANCELLED)
        self.assertEqual(cancelled.result.terminal_reason, "user_cancelled")
        self.assertEqual(recovered.result.status, AgentTurnStatus.FINISHED)
        self.assertEqual(recovered.result.terminal_reason, "agent_finished")
        self.assertEqual(timed_out.result.status, AgentTurnStatus.FAILED)
        self.assertEqual(timed_out.result.terminal_reason, "wall_clock_budget")
        self.assertEqual(recovered_after_timeout.result.status, AgentTurnStatus.FINISHED)
        self.assertEqual(chat.calls, 4)
        states = [event.payload.get("state") for event in
                  channel.events_after("", lease_id=lease) if event.kind == "progress"]
        self.assertIn("cancelled", states)
        self.assertIn("finished", states)

    def _run_messages(self, messages, executor):
        channel = executor.channel
        lease = getattr(executor, "lease")
        for message in messages:
            channel.accept_message(message, lease_id=lease)
        host = LiveConversationRunner(channel, executor=executor)
        holder = {}

        def target():
            try:
                holder["outcomes"] = host.run()
            except BaseException as exc:  # surfaced on the test thread below
                holder["error"] = exc

        worker = threading.Thread(target=target, daemon=True)
        worker.start()
        recorded = [host.next_outcome(timeout_s=2) for _message in messages]
        host.close("test_complete")
        worker.join(2)
        self.assertFalse(worker.is_alive(), "conversation host did not stop after close")
        if "error" in holder:
            raise holder["error"]
        self.assertEqual(tuple(recorded), holder["outcomes"])
        return host, holder["outcomes"]

    def _executor(self, *, failures=None, results=None):
        channel, lease = _channel_with_observation()
        executor = _RecordingExecutor(
            channel, failures=failures, results=results)
        executor.lease = lease
        return executor

    def test_one_executor_instance_receives_three_real_wire_turns_in_order(self):
        messages = [_message(1), _message(2), _message(3)]
        executor = self._executor()
        host, outcomes = self._run_messages(messages, executor)

        self.assertEqual([call["message"] for call in executor.calls], messages)
        self.assertEqual(
            [(call["observation_id"], call["page_epoch"], call["page_url"])
             for call in executor.calls],
            [("observation-1", 1, ORIGIN + "/companies")] * 3,
        )
        self.assertEqual([outcome.turn for outcome in outcomes], [1, 2, 3])
        self.assertEqual(host.turns_completed, 3)
        self.assertEqual(
            [outcome.result["message_id"] for outcome in outcomes],
            [message["message_id"] for message in messages],
        )

    def test_second_turn_exception_is_isolated_and_third_turn_still_runs(self):
        messages = [_message(1), _message(2), _message(3)]
        executor = self._executor(
            failures={"message-2": ValueError("broken turn")},
        )
        _, outcomes = self._run_messages(messages, executor)

        self.assertEqual([call["message"]["message_id"] for call in executor.calls],
                         ["message-1", "message-2", "message-3"])
        self.assertEqual(len(outcomes), 3)
        self.assertIsNone(outcomes[1].result)
        self.assertIn("ValueError: broken turn", outcomes[1].error)
        self.assertEqual(outcomes[2].result, {"message_id": "message-3"})
        events = executor.channel.events_after("", lease_id=executor.lease)
        failures = [event for event in events
                    if event.kind == "progress"
                    and event.payload.get("state") == "task_failed"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(
            failures[0].payload["message"], "本次任务执行失败，请查看运行记录。")

    def test_typed_retryable_turn_error_keeps_recovery_guidance(self):
        messages = [_message(1), _message(2)]
        executor = self._executor(failures={
            "message-1": AgentTurnError(
                AgentTurnErrorCode.STALE_MESSAGE,
                "internal stale detail",
                retryable=True,
            ),
        })
        _, outcomes = self._run_messages(messages, executor)

        self.assertIsNone(outcomes[0].result)
        self.assertEqual(outcomes[1].result, {"message_id": "message-2"})
        events = executor.channel.events_after("", lease_id=executor.lease)
        failure = [event for event in events
                   if event.kind == "progress"
                   and event.payload.get("state") == "task_failed"][0]
        self.assertEqual(failure.payload["error_code"], "stale_message")
        self.assertTrue(failure.payload["retryable"])
        self.assertEqual(
            failure.payload["message"], "页面已发生变化，请检查当前页面后重新提交。")

    def test_remote_session_close_normally_ends_the_host(self):
        executor = self._executor()
        host = LiveConversationRunner(executor.channel, executor=executor)
        holder = {}
        worker = threading.Thread(
            target=lambda: holder.setdefault("outcomes", host.run()), daemon=True)
        worker.start()
        host.close("user_closed")
        worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(holder["outcomes"], ())
        self.assertEqual(executor.calls, [])
        self.assertEqual(host.turns_completed, 0)

    def test_indeterminate_result_is_returned_unchanged_and_never_retried(self):
        result = _TurnResult(
            status="indeterminate", terminal_reason="indeterminate_write")
        executor = self._executor(results={"message-1": result})
        _, outcomes = self._run_messages([_message(1)], executor)

        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(len(outcomes), 1)
        self.assertIs(outcomes[0].result, result)
        self.assertEqual(outcomes[0].result.status, "indeterminate")
        self.assertEqual(outcomes[0].result.terminal_reason, "indeterminate_write")
        events = executor.channel.events_after("", lease_id=executor.lease)
        self.assertFalse(any(
            event.kind == "progress" and event.payload.get("state") == "task_failed"
            for event in events))

    def test_remote_session_close_from_executor_is_not_a_failed_turn(self):
        executor = self._executor(
            failures={"message-1": RemoteSessionClosed("panel closed")})
        executor.channel.accept_message(_message(1), lease_id=executor.lease)
        host = LiveConversationRunner(executor.channel, executor=executor)
        holder = {}
        worker = threading.Thread(
            target=lambda: holder.setdefault("outcomes", host.run()), daemon=True)
        worker.start()
        worker.join(2)
        host.close("test_complete")
        worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(holder["outcomes"], ())
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(host.turns_completed, 0)
        events = executor.channel.events_after("", lease_id=executor.lease)
        self.assertFalse(any(
            event.kind == "progress" and event.payload.get("state") == "task_failed"
            for event in events))

    def test_legacy_per_turn_factory_remains_a_thin_compatibility_path(self):
        channel, lease = _channel_with_observation()
        message = _message(1)
        channel.accept_message(message, lease_id=lease)
        calls = []

        def factory(received, turn):
            calls.append((received, turn))
            return _TurnRunner(value=turn)

        host = LiveConversationRunner(channel, runner_factory=factory)
        holder = {}
        worker = threading.Thread(
            target=lambda: holder.setdefault("outcomes", host.run_once()), daemon=True)
        worker.start()
        recorded = host.next_outcome(timeout_s=2)
        host.close("test_complete")
        worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(calls, [(message, 1)])
        self.assertEqual(holder["outcomes"], (recorded,))
        self.assertEqual(recorded.result, {"message": "Task 1", "value": 1})

    def test_owns_gateway_bootstrap_worker_and_status_without_exposing_token(self):
        live = LiveProductSession(
            allowed_origin=ORIGIN,
            page_controller_module=ORIGIN + "/page-controller.js",
            runner_factory=lambda _channel: _Runner({"ok": True}),
            session_id="live-test",
            sensitive_query_keys=frozenset({"handoffKey"}),
        )
        with self.assertRaisesRegex(RuntimeError, "started"):
            _ = live.bootstrap_script
        try:
            live.start()
            source = live.bootstrap_script
            result = live.wait(2)
            status = live.status()
            self.assertEqual(result, {"ok": True})
            self.assertTrue(status.finished)
            self.assertFalse(status.running)
            self.assertFalse(status.failed)
            self.assertNotIn(live.grant.client_token, repr(status))
            self.assertIn(live.grant.client_token, source)
            self.assertIn(live.gateway.runtime_url, source)
            self.assertIn("window.__WOA_EMBED_CONFIG__ =", source)
            self.assertIn('"sensitiveQueryKeys":["handoffkey"]', source)
        finally:
            live.close()

    def test_worker_failure_is_reported_at_the_product_boundary(self):
        live = LiveProductSession(
            allowed_origin=ORIGIN,
            page_controller_module=ORIGIN + "/page-controller.js",
            runner_factory=lambda _channel: _Runner(error=ValueError("broken runner")),
        )
        try:
            live.start()
            with self.assertRaisesRegex(RuntimeError, "ValueError: broken runner"):
                live.wait(2)
            self.assertTrue(live.status().failed)
        finally:
            live.close()

    def test_a_worker_that_dies_takes_its_session_with_it(self):
        """P7.5 CR-44: a session with no worker must not go on accepting questions.

        The worker is the only thing that answers a message. When it ended -- here because
        it raised, in the field because a wait timed out -- the session stayed open, the
        panel stayed connected, and every question the person submitted was accepted by a
        session with nobody left to run it. They saw their question and then nothing.
        """
        live = LiveProductSession(
            allowed_origin=ORIGIN,
            page_controller_module=ORIGIN + "/page-controller.js",
            runner_factory=lambda _channel: _Runner(error=ValueError("broken runner")),
        )
        try:
            live.start()
            with self.assertRaises(RuntimeError):
                live.wait(2)
            for _ in range(50):
                try:
                    live.channel.validate_live()
                except RemoteSessionClosed:
                    break
                time.sleep(0.02)
            with self.assertRaises(RemoteSessionClosed):
                live.channel.validate_live()
            self.assertFalse(live.readiness_snapshot()["ready"])
        finally:
            live.close()

    def test_shared_server_hosts_and_revokes_independent_sessions(self):
        server = LiveProductServer(max_sessions=2, sse_wait_s=.01)
        try:
            first = server.create_session(
                allowed_origin=ORIGIN,
                page_controller_module=ORIGIN + "/page-controller.js",
                runner_factory=lambda _channel: _Runner({"session": 1}),
                session_id="shared-1")
            second = server.create_session(
                allowed_origin=ORIGIN,
                page_controller_module=ORIGIN + "/page-controller.js",
                runner_factory=lambda _channel: _Runner({"session": 2}),
                session_id="shared-2")
            first.start()
            second.start()

            self.assertIs(first.gateway, second.gateway)
            self.assertEqual(first.gateway.base_url, second.gateway.base_url)
            self.assertEqual(server.session_count, 2)
            self.assertEqual(server.registry.session_count, 2)
            self.assertEqual(first.wait(2), {"session": 1})
            self.assertEqual(second.wait(2), {"session": 2})
            first.close()
            self.assertEqual(server.session_count, 1)
            self.assertEqual(server.registry.session_count, 1)
            with self.assertRaisesRegex(Exception, "unknown session"):
                server.registry.authenticate(
                    first.grant.session_id, first.grant.client_token, ORIGIN)
            self.assertIs(
                server.registry.authenticate(
                    second.grant.session_id, second.grant.client_token, ORIGIN),
                second.channel)
        finally:
            server.close()
        self.assertEqual(server.session_count, 0)
        self.assertEqual(server.registry.session_count, 0)

    def test_shared_server_enforces_session_capacity(self):
        server = LiveProductServer(max_sessions=1, sse_wait_s=.01)
        try:
            first = server.create_session(
                allowed_origin=ORIGIN,
                page_controller_module=ORIGIN + "/page-controller.js",
                runner_factory=lambda _channel: _Runner(),
                session_id="capacity-1")
            with self.assertRaisesRegex(RuntimeError, "capacity"):
                server.create_session(
                    allowed_origin=ORIGIN,
                    page_controller_module=ORIGIN + "/page-controller.js",
                    runner_factory=lambda _channel: _Runner(),
                    session_id="capacity-2")
            first.close()
            replacement = server.create_session(
                allowed_origin=ORIGIN,
                page_controller_module=ORIGIN + "/page-controller.js",
                runner_factory=lambda _channel: _Runner(),
                session_id="capacity-2")
            self.assertEqual(replacement.grant.session_id, "capacity-2")
        finally:
            server.close()

    def test_shared_server_enforces_capacity_per_stable_actor(self):
        now = int(time.time() * 1000)

        def actor(actor_id, delegation_id):
            return PrincipalContext(
                schema_version=1, application_id="twenty",
                tenant_id="tenant-1", workspace_id="workspace-1",
                actor_id=actor_id, delegation_id=delegation_id,
                issued_at=now, expires_at=now + 60_000,
                scopes=("companies:read",))

        server = LiveProductServer(
            max_sessions=4, max_sessions_per_actor=2, sse_wait_s=.01)
        try:
            sessions = [server.create_session(
                allowed_origin=ORIGIN,
                page_controller_module=ORIGIN + "/page-controller.js",
                runner_factory=lambda _channel: _Runner(),
                principal=actor("actor-1", f"delegation-{index}"),
                session_id=f"actor-capacity-{index}") for index in range(2)]
            with self.assertRaisesRegex(RuntimeError, "actor session capacity"):
                server.create_session(
                    allowed_origin=ORIGIN,
                    page_controller_module=ORIGIN + "/page-controller.js",
                    runner_factory=lambda _channel: _Runner(),
                    principal=actor("actor-1", "fresh-delegation"),
                    session_id="actor-capacity-refused")
            other = server.create_session(
                allowed_origin=ORIGIN,
                page_controller_module=ORIGIN + "/page-controller.js",
                runner_factory=lambda _channel: _Runner(),
                principal=actor("actor-2", "delegation-other"),
                session_id="actor-capacity-other")
            self.assertEqual(server.session_count, 3)
            sessions[0].close()
            other.close()
        finally:
            server.close()

    def test_browser_inactivity_releases_capacity_without_owner_heartbeat(self):
        server = LiveProductServer(
            max_sessions=1, browser_idle_timeout_s=30, sse_wait_s=.01)
        try:
            first = server.create_session(
                allowed_origin=ORIGIN,
                page_controller_module=ORIGIN + "/page-controller.js",
                runner_factory=lambda _channel: _Runner(),
                session_id="browser-idle")
            activity_ms = first.channel.last_client_activity_ms
            # Durable owner validation is an internal heartbeat and must not count as
            # browser activity.
            first.channel.validate_live()
            self.assertEqual(first.channel.last_client_activity_ms, activity_ms)
            with mock.patch("web_operation_agent.composition.live.time.time",
                            return_value=(activity_ms + 30_000) / 1000):
                self.assertEqual(server.session_count, 0)
            replacement = server.create_session(
                allowed_origin=ORIGIN,
                page_controller_module=ORIGIN + "/page-controller.js",
                runner_factory=lambda _channel: _Runner(),
                session_id="browser-reconnected")
            self.assertEqual(replacement.grant.session_id, "browser-reconnected")
        finally:
            server.close()

    def test_server_readiness_reports_capacity_and_provider_circuit_without_content(self):
        provider = {
            "max_concurrent_requests": 4,
            "in_flight": 1,
            "consecutive_transient_failures": 0,
            "circuit_open": False,
            "retry_after_s": 0.0,
        }
        server = LiveProductServer(
            max_sessions=1, sse_wait_s=.01,
            provider_status=lambda: dict(provider))
        try:
            self.assertEqual(server.readiness_snapshot(), {
                "ready": True,
                "accepting_sessions": True,
                "session_count": 0,
                "session_capacity": 1,
                "provider": provider,
            })
            session = server.create_session(
                allowed_origin=ORIGIN,
                page_controller_module=ORIGIN + "/page-controller.js",
                runner_factory=lambda _channel: _Runner(),
                session_id="readiness-capacity")
            self.assertFalse(server.readiness_snapshot()["accepting_sessions"])
            provider["circuit_open"] = True
            provider["retry_after_s"] = 10.0
            snapshot = server.readiness_snapshot()
            self.assertFalse(snapshot["ready"])
            self.assertFalse(snapshot["accepting_sessions"])
            session.close()
        finally:
            server.close()

    def test_expired_sessions_release_shared_server_capacity(self):
        limits = RemoteSessionLimits(session_ttl_s=60)
        with mock.patch("web_operation_agent.transport.channel._now_ms", return_value=1_000):
            server = LiveProductServer(
                max_sessions=1, sse_wait_s=.01, session_limits=limits)
            first = server.create_session(
                allowed_origin=ORIGIN,
                page_controller_module=ORIGIN + "/page-controller.js",
                runner_factory=lambda _channel: _Runner(),
                session_id="expiring-capacity")
        try:
            self.assertEqual(first.grant.expires_at, 0)
            self.assertEqual(first.grant.session_expires_at, 61_000)
            with mock.patch("web_operation_agent.transport.channel._now_ms", return_value=61_000):
                self.assertEqual(server.session_count, 0)
                replacement = server.create_session(
                    allowed_origin=ORIGIN,
                    page_controller_module=ORIGIN + "/page-controller.js",
                    runner_factory=lambda _channel: _Runner(),
                    session_id="replacement-capacity")
                self.assertEqual(replacement.grant.session_id, "replacement-capacity")
        finally:
            server.close()

    def test_fifty_shared_sessions_keep_capabilities_and_channels_isolated(self):
        server = LiveProductServer(max_sessions=50, sse_wait_s=.01)
        try:
            sessions = [server.create_session(
                allowed_origin=ORIGIN,
                page_controller_module=ORIGIN + "/page-controller.js",
                runner_factory=lambda _channel: _Runner(),
                session_id=f"isolation-{index}") for index in range(50)]
            self.assertEqual(len({item.grant.client_token for item in sessions}), 50)
            for index, session in enumerate(sessions):
                self.assertIs(server.registry.authenticate(
                    session.grant.session_id, session.grant.client_token, ORIGIN),
                    session.channel)
                wrong = sessions[(index + 1) % len(sessions)].grant.client_token
                with self.assertRaisesRegex(RemoteProtocolError, "invalid session credential"):
                    server.registry.authenticate(
                        session.grant.session_id, wrong, ORIGIN)
            for session in sessions[::2]:
                session.close()
            self.assertEqual(server.session_count, 25)
            self.assertEqual(server.registry.session_count, 25)
            for session in sessions[1::2]:
                self.assertIs(server.registry.authenticate(
                    session.grant.session_id, session.grant.client_token, ORIGIN),
                    session.channel)
        finally:
            server.close()

    def test_live_result_can_be_projected_to_one_case_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = {
                "product_run_id": "live-run", "task": "Open orders",
                "product_success": True, "started_at": time.time() - 1,
                "finished_at": time.time(),
                "agent": {
                    "page_actions": 1, "terminal_reason": "agent_finished",
                    "agent_said": "Done", "spend": {"provider_calls": 1,
                    "cost_cny": .01}, "control": {"permit_consumed": 0,
                    "write_blocked": 0},
                },
            }
            (root / "result.json").write_text(json.dumps(result))
            (root / "session.jsonl").write_text(
                json.dumps({
                    "seq": 1, "t": result["started_at"], "run_id": "live-run",
                    "event": "product_verification", "product_success": True,
                }) + "\n")
            bundle = render_live_replay(
                result_path=root / "result.json", ledger_path=root / "session.jsonl",
                output_path=root / "replay.html", label="Live", mode="embedded")
            self.assertEqual(len(bundle["cases"]), 1)
            self.assertTrue((root / "replay.html").exists())
            self.assertTrue((root / "trace-data.json").exists())


if __name__ == "__main__":
    unittest.main()
