"""Transport-neutral contracts for the embedded Web Runtime boundary."""
import dataclasses
import unittest

from web_operation_agent import schemas as S


def _page(epoch: int, *, title: str = "Page") -> S.PageObservation:
    return S.PageObservation(
        schema_version=S.SCHEMA_VERSIONS["PageObservation"],
        epoch=epoch,
        url=f"https://app.example.test/page/{epoch}",
        title=title,
        landmarks=[title],
        elements=[S.ElementRef(f"e{epoch}", "button", "Continue", "button", epoch)],
    )


def _observation(epoch: int, observation_id: str, *, action_id: str = "") \
        -> S.ObservationEnvelope:
    return S.ObservationEnvelope(
        schema_version=S.SCHEMA_VERSIONS["ObservationEnvelope"],
        session_id="session-1",
        observation_id=observation_id,
        page_epoch=epoch,
        observation=_page(epoch),
        events=({"kind": "page_ready"},),
        caused_by_action_id=action_id,
    )


def _action() -> S.ActionEnvelope:
    command = S.ActionCommand(
        command_id="action-1",
        action="click",
        args={"ref": "e3", "epoch": 3},
        expect_navigation=True,
    )
    return S.ActionEnvelope(
        schema_version=S.SCHEMA_VERSIONS["ActionEnvelope"],
        session_id="session-1",
        turn_id="turn-1",
        action_id="action-1",
        based_on_observation_id="observation-3",
        page_epoch=3,
        deadline=2_000_000_000_000,
        command_digest=command.content_digest,
        command=command,
    )


def _completed_result() -> S.ActionResultEnvelope:
    after = _observation(4, "observation-4", action_id="action-1")
    receipt = S.ActionReceipt(
        schema_version=S.SCHEMA_VERSIONS["ActionReceipt"],
        command_id="action-1",
        action="click",
        args_echo={"ref": "e3", "epoch": 3},
        ok=True,
        url_before="https://app.example.test/page/3",
        url_after="https://app.example.test/page/4",
        navigated=True,
        epoch_before=3,
        epoch_after=4,
        elapsed_ms=20,
        observation=after.observation,
    )
    return S.ActionResultEnvelope(
        schema_version=S.SCHEMA_VERSIONS["ActionResultEnvelope"],
        session_id="session-1",
        turn_id="turn-1",
        action_id="action-1",
        based_on_observation_id="observation-3",
        page_epoch=3,
        command_digest=_action().command_digest,
        status="completed",
        receipt=receipt,
        observation_after=after,
    )


class ClientHandshake(unittest.TestCase):
    def test_round_trip_preserves_the_sse_resume_cursor(self):
        hello = S.ClientHello(
            schema_version=S.SCHEMA_VERSIONS["ClientHello"],
            session_id="session-1",
            client_id="browser-tab-1",
            runtime_instance_id="document-1",
            runtime="embedded_web",
            capabilities=("observe", "click", "fill"),
            page_origin="https://app.example.test",
            last_event_id="e00000017",
        )
        self.assertEqual(S.ClientHello.from_json(hello.to_json()), hello)
        self.assertEqual(hello.to_json()["last_event_id"], "e00000017")

    def test_origin_is_an_origin_not_a_page_or_credential_container(self):
        base = dict(
            schema_version=1,
            session_id="session-1",
            client_id="client-1",
            runtime_instance_id="document-1",
            runtime="embedded_web",
            capabilities=("observe",),
        )
        for origin in ("https://app.example.test/path", "https://u:p@app.example.test", "file:///x"):
            with self.subTest(origin=origin), self.assertRaises(S.SchemaError):
                S.ClientHello(page_origin=origin, **base)

    def test_capabilities_must_be_unique_strings(self):
        with self.assertRaises(S.SchemaError):
            S.ClientHello(schema_version=1, session_id="s", client_id="c",
                          runtime_instance_id="document-1", runtime="web",
                          capabilities=("observe", "observe"), page_origin="https://example.test")

    def test_resume_cursor_has_one_canonical_shape(self):
        base = dict(
            schema_version=1, session_id="s", client_id="c",
            runtime_instance_id="document-1", runtime="web", capabilities=("observe",),
            page_origin="https://example.test",
        )
        for cursor in ("event-17", "e17", "e000000017", "e00000000", 17):
            with self.subTest(cursor=cursor), self.assertRaises(S.SchemaError):
                S.ClientHello(last_event_id=cursor, **base)


class Observations(unittest.TestCase):
    def test_round_trip_preserves_identity_epoch_events_and_cause(self):
        observation = _observation(4, "observation-4", action_id="action-1")
        back = S.ObservationEnvelope.from_json(observation.to_json())
        self.assertEqual(back.to_json(), observation.to_json())
        self.assertEqual(back.content_digest, observation.content_digest)

    def test_outer_epoch_cannot_disagree_with_the_page(self):
        with self.assertRaises(S.SchemaError):
            S.ObservationEnvelope(schema_version=1, session_id="s", observation_id="o",
                                  page_epoch=4, observation=_page(3))

    def test_observation_owns_nested_page_and_event_values(self):
        page = _page(4)
        event = {"kind": "page_ready", "detail": {"values": ["original"]}}
        envelope = S.ObservationEnvelope(
            schema_version=1, session_id="s", observation_id="o", page_epoch=4,
            observation=page, events=(event,),
        )
        page.title = "mutated"
        event["detail"]["values"].append("mutated")
        wire = envelope.to_json()
        wire["observation"]["title"] = "wire mutation"
        wire["events"][0]["detail"]["values"].append("wire mutation")
        self.assertEqual(envelope.observation.title, "Page")
        self.assertEqual(envelope.events[0]["detail"]["values"], ["original"])


class AtomicActions(unittest.TestCase):
    def test_round_trip_keeps_all_correlation_and_staleness_fields(self):
        action = _action()
        back = S.ActionEnvelope.from_json(action.to_json())
        self.assertEqual(back.to_json(), action.to_json())
        self.assertEqual(back.idempotency_key, ("session-1", "action-1"))
        self.assertEqual(back.based_on_observation_id, "observation-3")
        self.assertEqual(back.page_epoch, 3)
        self.assertEqual(back.deadline, 2_000_000_000_000)

    def test_action_id_is_also_the_inner_command_id(self):
        action = _action()
        with self.assertRaises(S.SchemaError):
            S.ActionEnvelope(schema_version=1, session_id=action.session_id,
                             turn_id=action.turn_id, action_id="another-action",
                             based_on_observation_id=action.based_on_observation_id,
                             page_epoch=action.page_epoch, deadline=action.deadline,
                             command_digest=action.command_digest,
                             command=action.command)

    def test_epoch_and_deadline_are_integers_not_boolean_or_relative_text(self):
        action = _action().to_json()
        for field, value in (("page_epoch", True), ("deadline", 0), ("deadline", "soon")):
            invalid = dict(action)
            invalid[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(S.SchemaError):
                S.ActionEnvelope.from_json(invalid)

    def test_command_digest_detects_inner_command_changes(self):
        payload = _action().to_json()
        payload["command"]["args"]["ref"] = "e99"
        with self.assertRaisesRegex(S.SchemaError, "command_digest"):
            S.ActionEnvelope.from_json(payload)
        payload = _action().to_json()
        payload["command_digest"] = "A" * 64
        with self.assertRaisesRegex(S.SchemaError, "lowercase hex"):
            S.ActionEnvelope.from_json(payload)

    def test_action_owns_its_nested_command_arguments(self):
        args = {"ref": "e3", "nested": {"values": ["original"]}}
        command = S.ActionCommand("action-1", "click", args)
        action = S.ActionEnvelope(
            schema_version=1, session_id="session-1", turn_id="turn-1",
            action_id="action-1", based_on_observation_id="observation-3",
            page_epoch=3, deadline=2_000_000_000_000,
            command_digest=command.content_digest, command=command,
        )
        args["nested"]["values"].append("mutated")
        command.args["nested"]["values"].append("mutated")
        wire = action.to_json()
        wire["command"]["args"]["nested"]["values"].append("wire mutation")
        self.assertEqual(action.command.args["nested"]["values"], ["original"])


class ActionResults(unittest.TestCase):
    def test_completed_result_round_trips_as_the_same_idempotent_value(self):
        result = _completed_result()
        replay = S.ActionResultEnvelope.from_json(result.to_json())
        self.assertEqual(replay.idempotency_key, _action().idempotency_key)
        self.assertEqual(replay.content_digest, result.content_digest)
        self.assertEqual(replay.to_json(), result.to_json())

    def test_rejection_records_no_receipt_and_returns_a_fresh_observation(self):
        rejected = S.ActionResultEnvelope(
            schema_version=1,
            session_id="session-1",
            turn_id="turn-1",
            action_id="action-1",
            based_on_observation_id="observation-3",
            page_epoch=3,
            command_digest=_action().command_digest,
            status="rejected",
            receipt=None,
            observation_after=_observation(4, "observation-4", action_id="action-1"),
            error_code="stale_observation",
        )
        self.assertEqual(S.ActionResultEnvelope.from_json(rejected.to_json()), rejected)

    def test_indeterminate_result_does_not_invent_a_receipt_or_retry_signal(self):
        result = S.ActionResultEnvelope(
            schema_version=1,
            session_id="session-1",
            turn_id="turn-1",
            action_id="action-1",
            based_on_observation_id="observation-3",
            page_epoch=3,
            command_digest=_action().command_digest,
            status="indeterminate",
            receipt=None,
            observation_after=None,
            error_code="execution_state_unknown",
        )
        self.assertIsNone(S.ActionResultEnvelope.from_json(result.to_json()).receipt)

    def test_completed_result_requires_matching_fresh_observation(self):
        valid = _completed_result()
        cases = [
            dict(action_id="another-action"),
            dict(page_epoch=4),
            dict(observation_after=_observation(3, "observation-3", action_id="action-1")),
        ]
        for replacement in cases:
            values = dict(
                schema_version=valid.schema_version,
                session_id=valid.session_id,
                turn_id=valid.turn_id,
                action_id=valid.action_id,
                based_on_observation_id=valid.based_on_observation_id,
                page_epoch=valid.page_epoch,
                command_digest=valid.command_digest,
                status=valid.status,
                receipt=valid.receipt,
                observation_after=valid.observation_after,
            )
            values.update(replacement)
            with self.subTest(replacement=replacement), self.assertRaises(S.SchemaError):
                S.ActionResultEnvelope(**values)

    def test_completed_receipt_lifecycle_must_be_self_consistent(self):
        valid = _completed_result()
        cases = [
            dataclasses.replace(valid.receipt, url_after="https://app.example.test/wrong"),
            dataclasses.replace(valid.receipt, navigated=False),
            dataclasses.replace(valid.receipt, epoch_after=3),
            dataclasses.replace(valid.receipt, error_detail="success is not an error"),
            dataclasses.replace(valid.receipt, ok=False, error_code=""),
        ]
        for receipt in cases:
            with self.subTest(receipt=receipt), self.assertRaises(S.SchemaError):
                dataclasses.replace(valid, receipt=receipt)

    def test_result_requires_a_canonical_command_digest_echo(self):
        payload = _completed_result().to_json()
        for digest_value in ("", "0" * 63, "A" * 64, None):
            invalid = dict(payload, command_digest=digest_value)
            with self.subTest(digest=digest_value), self.assertRaises(S.SchemaError):
                S.ActionResultEnvelope.from_json(invalid)

    def test_remote_pre_dispatch_rejections_stay_retryable_agent_feedback(self):
        from web_operation_agent.session import ServerAgentSession
        codes = {
            "deadline_expired", "stale_observation", "capability_refused", "runtime_replaced",
        }
        self.assertLessEqual(codes, S.RECEIPT_ERRORS)
        self.assertLessEqual(codes, ServerAgentSession.PRE_DISPATCH_ERRORS)


class EventStream(unittest.TestCase):
    def test_action_event_has_an_ordered_resume_id_and_typed_payload(self):
        event = S.TaskEvent.for_action(event_id="e00000018", sequence=18, action=_action())
        back = S.TaskEvent.from_json(event.to_json())
        self.assertEqual(back.to_json(), event.to_json())
        self.assertEqual(back.action(), _action())
        self.assertEqual(back.event_id, "e00000018")
        self.assertEqual(back.sequence, 18)

    def test_action_event_cannot_smuggle_another_session_or_turn(self):
        action = _action()
        for field, value in (("session_id", "other-session"), ("turn_id", "other-turn")):
            values = dict(schema_version=1, event_id="e00000001", session_id=action.session_id,
                          sequence=1, kind="action", turn_id=action.turn_id,
                          payload=action.to_json())
            values[field] = value
            with self.subTest(field=field), self.assertRaises(S.SchemaError):
                S.TaskEvent(**values)

    def test_event_cursor_encodes_sequence_and_payload_is_owned(self):
        action = _action()
        with self.assertRaisesRegex(S.SchemaError, "encode"):
            S.TaskEvent.for_action(event_id="e00000002", sequence=1, action=action)
        payload = {"message": {"parts": ["original"]}}
        event = S.TaskEvent(1, "e00000001", "session-1", 1, "progress", "", payload)
        payload["message"]["parts"].append("mutated")
        wire = event.to_json()
        wire["payload"]["message"]["parts"].append("wire mutation")
        self.assertEqual(event.payload["message"]["parts"], ["original"])


class StrictWireParsing(unittest.TestCase):
    def test_every_remote_type_rejects_unknown_fields_and_versions(self):
        hello = S.ClientHello(
            schema_version=1, session_id="session-1", client_id="client-1",
            runtime_instance_id="document-1", runtime="embedded_web",
            capabilities=("observe",), page_origin="https://app.example.test")
        observation = _observation(4, "observation-4", action_id="action-1")
        action = _action()
        result = _completed_result()
        event = S.TaskEvent.for_action(event_id="e00000001", sequence=1, action=action)
        cancellation = S.CancellationRequest(1, "session-1", "message-1")
        cases = [
            (S.ClientHello, hello.to_json()),
            (S.ObservationEnvelope, observation.to_json()),
            (S.ActionEnvelope, action.to_json()),
            (S.ActionResultEnvelope, result.to_json()),
            (S.TaskEvent, event.to_json()),
            (S.CancellationRequest, cancellation.to_json()),
        ]
        for cls, payload in cases:
            with self.subTest(cls=cls.__name__, failure="unknown field"):
                invalid = dict(payload, surprise=True)
                with self.assertRaises(S.SchemaError):
                    cls.from_json(invalid)
            with self.subTest(cls=cls.__name__, failure="version"):
                invalid = dict(payload, schema_version=99)
                with self.assertRaises(S.SchemaError):
                    cls.from_json(invalid)

    def test_inner_wire_types_reject_unknown_fields_nulls_and_coercions(self):
        command = _action().command.to_json()
        element = _page(3).elements[0].to_json()
        page = _page(3).to_json()
        receipt = _completed_result().receipt.to_json()
        cases = [
            (S.ActionCommand, dict(command, surprise=True)),
            (S.ActionCommand, dict(command, args=None)),
            (S.ActionCommand, dict(command, expect_navigation=1)),
            (S.ElementRef, dict(element, surprise=True)),
            (S.ElementRef, dict(element, epoch=True)),
            (S.ElementRef, dict(element, attrs=None)),
            (S.PageObservation, dict(page, surprise=True)),
            (S.PageObservation, dict(page, landmarks="Heading")),
            (S.PageObservation, dict(page, messages="message")),
            (S.PageObservation, dict(page, truncated=0)),
            (S.ActionReceipt, dict(receipt, surprise=True)),
            (S.ActionReceipt, dict(receipt, args_echo=None)),
            (S.ActionReceipt, dict(receipt, ok=1)),
            (S.ActionReceipt, dict(receipt, elapsed_ms=True)),
        ]
        for cls, payload in cases:
            with self.subTest(cls=cls.__name__, payload=payload), self.assertRaises(S.SchemaError):
                cls.from_json(payload)

    def test_inner_values_and_serialized_copies_do_not_alias_callers(self):
        attrs = {"meta": {"values": ["original"]}}
        element = S.ElementRef("e1", "button", "Continue", "button", 1, attrs)
        args = {"nested": {"values": ["original"]}}
        command = S.ActionCommand("a1", "click", args)
        echo = {"nested": {"values": ["original"]}}
        receipt = S.ActionReceipt(1, "a1", "click", echo, True)
        attrs["meta"]["values"].append("mutated")
        args["nested"]["values"].append("mutated")
        echo["nested"]["values"].append("mutated")
        element_wire, command_wire, receipt_wire = (
            element.to_json(), command.to_json(), receipt.to_json())
        element_wire["attrs"]["meta"]["values"].append("wire mutation")
        command_wire["args"]["nested"]["values"].append("wire mutation")
        receipt_wire["args_echo"]["nested"]["values"].append("wire mutation")
        self.assertEqual(element.attrs["meta"]["values"], ["original"])
        self.assertEqual(command.args["nested"]["values"], ["original"])
        self.assertEqual(receipt.args_echo["nested"]["values"], ["original"])

    def test_boolean_schema_versions_are_not_integer_version_one(self):
        cases = [
            (S.ActionCommand, dict(_action().command.to_json(), schema_version=True)),
            (S.PageObservation, dict(_page(3).to_json(), schema_version=True)),
            (S.ActionReceipt, dict(_completed_result().receipt.to_json(), schema_version=True)),
        ]
        for cls, payload in cases:
            with self.subTest(cls=cls.__name__), self.assertRaises(S.SchemaError):
                cls.from_json(payload)


if __name__ == "__main__":
    unittest.main()
