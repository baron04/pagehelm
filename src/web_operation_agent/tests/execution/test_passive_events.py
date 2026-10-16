"""Passive observation cannot change decisions, leak values or hide uncertain dispatch."""
import json
import unittest
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

from web_operation_agent.business.dispatch_evidence import (
    mutation_evidence, mutation_transport, verified_after_readback)
from web_operation_agent.execution.events import TurnEvents, turn_interval
from web_operation_agent.execution.receipt import MeasuredChat, TurnReceipt
from web_operation_agent.tests.execution.test_receipt import Chat


class PassiveEventsTests(unittest.TestCase):
    def test_channel_acceptance_precedes_executor_context_without_duplicate_event(self):
        clock = [10]
        delivered = []
        events = TurnEvents(delivered.append, clock=lambda: clock[0])
        events.accepted("normal-turn")
        clock[0] = 20
        with events.turn("normal-turn"), turn_interval("application"):
            events.accepted("queued-turn")
            self.assertEqual(events.latest_turn_id, "normal-turn")
        snapshot = events.snapshot("normal-turn")
        self.assertEqual(snapshot["events"], [e for e in delivered if e["turn_id"] == "normal-turn"])
        self.assertEqual(snapshot["events"][0]["monotonic_ns"], 10)
        self.assertEqual(sum(e["kind"] == "request_received" for e in snapshot["events"]), 1)
        delivered[0]["kind"] = "observer-owned-copy"
        self.assertEqual(events.snapshot("normal-turn")["events"][0]["kind"], "request_received")

    def test_parallel_model_failure_closes_intervals_on_one_clock_without_content(self):
        class Fails(Chat):
            def complete(self, *args, **kwargs):
                raise RuntimeError("private failure")
        emitted = []
        events = TurnEvents(emitted.append)
        with events.turn("normal-turn"):
            receipt = TurnReceipt()
            model = MeasuredChat(Fails(), receipt, remaining_s=lambda: 10)
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(model.complete, [{"role": "user", "content": "private request"}], [], "run")
                    for _ in range(2)]
                for future in futures:
                    with self.assertRaises(RuntimeError):
                        future.result()
        self.assertEqual(len({e["clock_id"] for e in emitted}), 1)
        self.assertEqual({e["turn_id"] for e in emitted}, {"normal-turn"})
        self.assertEqual(sum(e["kind"] == "model_begin" for e in emitted), 2)
        for begin in [e for e in emitted if e["kind"].endswith("_begin")]:
            end, = [e for e in emitted if e["kind"] == begin["kind"].replace("_begin", "_end")
                and e["interval_id"] == begin["interval_id"]]
            self.assertGreaterEqual(end["monotonic_ns"], begin["monotonic_ns"])
        self.assertNotIn("private", json.dumps(emitted))

    def test_broken_observer_marks_missing_evidence_without_breaking_execution(self):
        def unavailable(event):
            event["kind"] = "modified"
            raise RuntimeError("observer unavailable")
        events = TurnEvents(unavailable)
        with events.turn("normal-turn"), turn_interval("page"):
            completed = True
        self.assertTrue(completed)
        self.assertFalse(events.snapshot("normal-turn")["delivery_complete"])
        self.assertEqual(events.snapshot("normal-turn")["events"][0]["kind"], "request_received")

    def test_actual_transport_is_distinct_from_claim_and_survives_lost_readback(self):
        events = TurnEvents()
        proposal = SimpleNamespace(content_digest="digest")
        approval = SimpleNamespace(approval_id="approval")
        context = SimpleNamespace(operation_id="authorized-effect")
        with events.turn("normal-turn"):
            with mutation_evidence(proposal, approval, context) as evidence:
                evidence.claimed()  # Native prechecks refused before any transport.
            with self.assertRaises(RuntimeError), mutation_evidence(proposal, approval, context) as evidence:
                evidence.claimed()
                with mutation_transport() as transport:
                    transport.applied()
                raise RuntimeError("lost readback")
        before, after = events.snapshot("normal-turn")["receipts"]
        self.assertEqual((before["dispatch_state"], before["dispatch_count"], before["effect_status"]),
            ("not-dispatched", 0, "not-applicable"))
        self.assertEqual((after["dispatch_state"], after["dispatch_count"], after["effect_status"]),
            ("dispatched", 1, "applied"))
        self.assertEqual(before["operation_id"], after["operation_id"])

    def test_unknown_component_duplicate_and_unbound_transports_remain_visible(self):
        events = TurnEvents()
        with events.turn("normal-turn"):
            with mutation_evidence(SimpleNamespace(content_digest="digest"),
                    SimpleNamespace(approval_id="approval"), SimpleNamespace(operation_id="effect")) as evidence:
                evidence.claimed()
                with mutation_transport("first") as transport:
                    transport.applied()
                with mutation_transport("second"):
                    pass  # An uncertain second component is not silently applied.
                with mutation_transport("first") as transport:
                    transport.applied()  # A duplicate is not hidden by component naming.
            with mutation_transport():
                pass
        observed = events.snapshot("normal-turn")
        receipt, = observed["receipts"]
        self.assertEqual(receipt["dispatch_count"], 2)
        self.assertEqual(receipt["effect_status"], "unknown")
        self.assertEqual(len(receipt["components"]), 3)
        self.assertEqual(observed["unbound_mutations"], 1)

    def test_exact_actor_readback_can_prove_one_uncertain_transport(self):
        events = TurnEvents()
        with events.turn("normal-turn"):
            with mutation_evidence(SimpleNamespace(content_digest="digest"),
                    SimpleNamespace(approval_id="approval"), SimpleNamespace(operation_id="effect")):
                with mutation_transport():
                    pass  # The response was lost after native dispatch.
                verified_after_readback()
                with self.assertRaises(ValueError):
                    verified_after_readback()  # Proof is consumed once.
        receipt, = events.snapshot("normal-turn")["receipts"]
        self.assertEqual(receipt["effect_status"], "applied")
        self.assertEqual(receipt["components"][0]["proof"], "actor_readback")
