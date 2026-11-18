"""Unit tests for the unified application-neutral write safety pipeline."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest

from web_operation_agent.business.approvals import ApprovalWatch
from web_operation_agent.business.control import ApprovalGrant
from web_operation_agent.business.convention_writes import (
    ConventionFieldSpec,
    ConventionStatusHandler,
    ConventionUpdateHandler,
    ConventionUpdateSpec,
    TwoStateStatusSpec,
)
from web_operation_agent.business.effects import EffectProjection, RequestedEffect, canonical
from web_operation_agent.business.errors import (
    BusinessMutationBlockedError,
    BusinessMutationIndeterminate,
    BusinessMutationRefusedError,
    BusinessToolError,
    BusinessToolRequestError,
)
from web_operation_agent.business.mutation import MemoryMutationJournal
from web_operation_agent.business.tools.contracts import BusinessMutationProposal, BusinessToolResult
from web_operation_agent.business.write_pipeline import (
    FieldDiff,
    InflightWriteTracker,
    ProposalDiff,
    WritePipeline,
    change_detail,
    check_append_preservation,
    create_detail,
    execute_verified_handler_write,
    normalize_approval_details,
    preserved_detail,
    require_append_preservation,
    side_effect_detail,
    verify_effect_projection,
    verify_preservation_snapshot,
    verify_preserves_unmentioned,
    verify_value_sources,
)
from web_operation_agent.business.write_service import BusinessWriteService
from web_operation_agent.execution.value_source import Entry, SourceLedger
from web_operation_agent.schemas.approval_details import ApprovalDetail
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent.tests.support.business import _operation_context


class _FakeConventionPort:
    def __init__(self, record: dict, status_record: dict | None = None):
        self.record = dict(record)
        self.status_record = dict(status_record or {"id": 2, "name": "Widget", "status": 0})
        self.dispatches = 0
        self.invalidated = []
        self.readback_error: Exception | None = None
        self.dispatch_error: Exception | None = None

    def update_specs(self):
        return {
            "items": ConventionUpdateSpec(
                entity="items",
                label="Item",
                fields=(
                    ConventionFieldSpec("name", "string", label="Name", required=True),
                    ConventionFieldSpec("note", "string", label="Note", required=False),
                ),
                volatile_fields=frozenset({"updatedAt"}),
            )
        }

    def status_specs(self):
        return {
            "widgets": TwoStateStatusSpec(
                entity="widgets",
                label="Widget",
                states=(("enabled", 0), ("disabled", 1)),
                volatile_fields=frozenset({"updatedAt"}),
            )
        }

    def verify_actor(self, context) -> str:
        return "actor-1"

    def resolve_target(self, entity: str, target: str, *, spec) -> dict:
        rec = self.record if entity == "items" else self.status_record
        if str(rec.get("name")) != target:
            raise BusinessToolRequestError("target not found")
        return dict(rec)

    def read_record(self, entity: str, record_id):
        if self.readback_error is not None:
            raise self.readback_error
        rec = self.record if entity == "items" else self.status_record
        return dict(rec) if str(rec.get("id")) == str(record_id) else None

    def encode_field(self, spec, field, raw_value, current_record):
        return raw_value, str(current_record.get(field.name) or ""), str(raw_value), None

    def recheck_bindings(self, bindings, *, actor_id: str) -> None:
        return None

    def dispatch_update(self, spec, record_id, payload: dict) -> None:
        self.dispatches += 1
        if self.dispatch_error is not None:
            raise self.dispatch_error
        self.record.update(payload)

    def dispatch_status(self, spec, record_id, status_code) -> None:
        self.dispatches += 1
        if self.dispatch_error is not None:
            raise self.dispatch_error
        self.status_record["status"] = status_code

    def invalidate_reads(self, entities: frozenset[str]) -> None:
        self.invalidated.append(entities)


class WritePipelineDiffAndInvariantTests(unittest.TestCase):
    def test_proposal_diff_normalizes_field_diffs_and_validates_bounds(self):
        diff = ProposalDiff(
            targets=("Item #1",),
            changes=(
                FieldDiff("Name", "New Name", before="Old Name"),
                create_detail("Tag", "alpha"),
            ),
            preserved=(preserved_detail("Owner", "actor-1"),),
            side_effects=(side_effect_detail("Index", "Updates search index"),),
        )
        details = diff.details()
        self.assertEqual(
            details,
            (
                ApprovalDetail("change", "Name", "Old Name → New Name"),
                ApprovalDetail("change", "Tag", "alpha"),
                ApprovalDetail("preserved", "Owner", "actor-1"),
                ApprovalDetail("side_effect", "Index", "Updates search index"),
            ),
        )
        round_trip = ProposalDiff.from_details(("Item #1",), details)
        self.assertEqual(round_trip.details(), details)
        self.assertEqual(len(round_trip.changes), 2)
        self.assertEqual(len(round_trip.preserved), 1)
        self.assertEqual(len(round_trip.side_effects), 1)

        with self.assertRaises(ValueError):
            normalize_approval_details([ApprovalDetail("change", "k", "x" * 32001)])
        with self.assertRaises(ValueError):
            ProposalDiff(targets=())

    def test_value_source_and_append_preservation_rejection(self):
        ledger = SourceLedger()
        ledger.add(Entry("Name", "Invented Name"))
        verdicts, missing = verify_value_sources(ledger, ["Update item name"])
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(len(missing), 1)

        overwritten = check_append_preservation(
            "请在备注后追加：加急处理",
            ((("备注", "note"), "已有原备注", "加急处理"),),
        )
        self.assertEqual(overwritten, ["备注"])
        with self.assertRaises(BusinessToolRequestError):
            require_append_preservation(
                "请在备注后追加：加急处理",
                ((("备注", "note"), "已有原备注", "加急处理"),),
            )
        # Preserving original prefix passes cleanly.
        require_append_preservation(
            "请在备注后追加：加急处理",
            ((("备注", "note"), "已有原备注", "已有原备注；加急处理"),),
        )

    def test_effect_projection_and_unmentioned_preservation_checks(self):
        change = RequestedEffect("items", "update", "items", "Item #1", canonical({"name": "Beta"}))
        projection = EffectProjection(
            change,
            canonical([]),
            canonical({"note": "keep"}),
            canonical({}),
            ("record",),
            preserves_unmentioned=True,
        )
        proposal = BusinessMutationProposal(
            tool="update_item",
            args={"name": "Beta"},
            effect="Update item",
            targets=("Item #1",),
            nonce="1" * 32,
            effect_projection=projection,
        )
        verify_effect_projection(proposal, replace(change, preserve=("note", "*")))
        verify_preserves_unmentioned(proposal, ("note",))

        with self.assertRaises(BusinessToolError):
            verify_effect_projection(replace(proposal, effect_projection=None), change)
        with self.assertRaises(BusinessToolRequestError):
            verify_preserves_unmentioned(
                replace(proposal, effect_projection=replace(projection, preserves_unmentioned=False))
            )


class WritePipelineExecutionTests(unittest.TestCase):
    def _approvals(self, tmp_dir: str, *, decision: str = "approved"):
        context = _operation_context()
        ledger = SessionLedger(Path(tmp_dir) / "session.jsonl", "run-pipe")

        def decide(proposal, *, context=None, offer_prefill=False):
            return ApprovalGrant(
                approval_id="ap-1",
                decision=decision,
                plan_digest=proposal.content_digest,
                operation_context_digest=context.binding_digest if context else "",
                decided_at=1.0,
            )

        approvals = ApprovalWatch(
            ledger=ledger,
            run_id="run-pipe",
            principal=context.principal,
            session_id=context.session_id,
            turn_id=context.turn_id,
            business_approval=decide,
            cancelled=lambda: False,
        )
        approvals.start_turn(context.expires_at - 1)
        return context, ledger, approvals

    def test_pre_dispatch_reread_drift_aborts_without_dispatch_on_convention_routes(self):
        port = _FakeConventionPort(
            {"id": 1, "name": "Alpha", "note": "Keep", "updatedAt": 100},
            {"id": 2, "name": "Widget", "status": 0, "updatedAt": 100},
        )
        update_handler = ConventionUpdateHandler(port)
        spec_u = update_handler.prepare(
            {"entity": "items", "target": "Alpha", "changes": [{"field": "name", "value": "Alpha2"}]}
        )
        # Underlying record drifts before dispatch (non-volatile field changes)
        port.record["note"] = "Mutated concurrently"
        with self.assertRaises(BusinessMutationRefusedError) as caught:
            update_handler.dispatch(spec_u.args)
        self.assertEqual(caught.exception.reason, "precondition_changed")
        self.assertEqual(port.dispatches, 0)

        status_handler = ConventionStatusHandler(port)
        spec_s = status_handler.prepare(
            {"entity": "widgets", "target": "Widget", "status": "disabled"}
        )
        port.status_record["name"] = "Widget Renamed"
        with self.assertRaises(BusinessMutationRefusedError) as caught_s:
            status_handler.dispatch(spec_s.args)
        self.assertEqual(caught_s.exception.reason, "precondition_changed")
        self.assertEqual(port.dispatches, 0)

    def test_single_use_approval_and_single_flight_block_replay_and_concurrency(self):
        with tempfile.TemporaryDirectory() as tmp:
            context, ledger, approvals = self._approvals(tmp)
            proposal = BusinessMutationProposal(
                tool="update_item",
                args={"name": "Beta"},
                effect="Update item",
                targets=("Item #1",),
                nonce="2" * 32,
                operation_context_digest=context.binding_digest,
                details=(change_detail("Name", "Alpha", "Beta"),),
            )
            dispatches = 0

            class _Runtime:
                def prepare_mutation(self, *a, **kw):
                    return proposal

                def execute_mutation(self, prop, approved_digest, loaded_ids, *, context=None, on_claimed=None):
                    nonlocal dispatches
                    if on_claimed:
                        on_claimed()
                    dispatches += 1
                    return BusinessToolResult(
                        {"verified": True, "entity": "items", "record_id": "1"},
                        {"verified": True},
                    )

            service = BusinessWriteService(_Runtime(), approvals, set())
            outcome = service.run("update_item", {"name": "Beta"}, step=1, context=context)
            self.assertEqual(outcome.state, "completed")
            self.assertEqual(dispatches, 1)

            # Re-dispatching the already consumed proposal must fail single-flight check
            with self.assertRaises(BusinessMutationBlockedError) as blocked:
                service.dispatch(
                    proposal,
                    approvals.approve_mutation(
                        proposal, step=2, tool=proposal.tool, context=context, identity={}
                    ),
                    step=2,
                    context=context,
                    identity={},
                )
            self.assertEqual(blocked.exception.reason, "already_consumed")
            self.assertEqual(dispatches, 1)

            # Concurrent single-flight check on InflightWriteTracker
            tracker = InflightWriteTracker()
            entered = threading.Event()
            release = threading.Event()
            errors: list[Exception] = []

            def worker():
                with tracker.single_flight("k1"):
                    entered.set()
                    release.wait(2)

            t = threading.Thread(target=worker)
            t.start()
            self.assertTrue(entered.wait(2))
            try:
                with self.assertRaises(BusinessMutationBlockedError):
                    with tracker.single_flight("k1"):
                        pass
            except Exception as exc:  # pragma: no cover
                errors.append(exc)
            finally:
                release.set()
                t.join(2)
            self.assertEqual(errors, [])

    def test_indeterminate_dispatch_and_readback_timeout_never_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            context, ledger, approvals = self._approvals(tmp)
            proposal = BusinessMutationProposal(
                tool="create_item",
                args={"name": "Beta"},
                effect="Create item",
                targets=("Beta",),
                nonce="3" * 32,
                operation_context_digest=context.binding_digest,
                details=(create_detail("Name", "Beta"),),
            )
            dispatches = 0

            class _TimeoutRuntime:
                def prepare_mutation(self, *a, **kw):
                    return proposal

                def execute_mutation(self, prop, approved_digest, loaded_ids, *, context=None, on_claimed=None):
                    nonlocal dispatches
                    if on_claimed:
                        on_claimed()
                    dispatches += 1
                    raise BusinessMutationIndeterminate("upstream timeout", phase="dispatch")

            service = BusinessWriteService(_TimeoutRuntime(), approvals, set())
            outcome = service.run("create_item", {"name": "Beta"}, step=1, context=context)
            self.assertEqual(outcome.state, "indeterminate")
            self.assertEqual(outcome.reason, "indeterminate_write")
            self.assertEqual(dispatches, 1)
            self.assertEqual(approvals.business_counts["mutation_indeterminate"], 1)
            self.assertTrue(
                any(ev["event"] == "business_mutation_indeterminate" for ev in ledger.events)
            )

        # Handler-level readback timeout after dispatch never retries dispatch
        port = _FakeConventionPort({"id": 1, "name": "Alpha", "note": "Keep", "updatedAt": 100})
        handler = ConventionUpdateHandler(port)
        spec = handler.prepare(
            {"entity": "items", "target": "Alpha", "changes": [{"field": "name", "value": "Alpha2"}]}
        )

        orig_read = port.read_record
        calls = {"reads": 0}

        def timeout_on_readback(entity, record_id):
            calls["reads"] += 1
            if calls["reads"] >= 2:
                raise TimeoutError("readback timed out")
            return orig_read(entity, record_id)

        port.read_record = timeout_on_readback
        with self.assertRaises(BusinessMutationIndeterminate) as caught:
            handler.dispatch(spec.args)
        self.assertEqual(caught.exception.phase, "postcondition")
        self.assertEqual(port.dispatches, 1)

    def test_same_actor_readback_verification_and_uncertain_recovery(self):
        # 1. Uncertain dispatch with successful same-actor readback recovers cleanly
        dispatch_calls = {"n": 0}
        state = {"id": 1, "name": "Before", "note": "Keep"}

        def dispatch_uncertain(_actor, _rs):
            dispatch_calls["n"] += 1
            state["name"] = "After"
            raise BusinessMutationIndeterminate("lost ack", phase="dispatch")

        def verify_ok(_actor, before_snap, _dr, _uncertain):
            drifted = verify_preservation_snapshot(before_snap, state, ignore=frozenset({"name"}))
            self.assertEqual(drifted, [])
            self.assertEqual(state["name"], "After")
            return BusinessToolResult({"verified": True}, {"verified": True})

        res = execute_verified_handler_write(
            verify_actor=lambda: "actor-1",
            pre_dispatch_reread=lambda _a: dict(state),
            dispatch_once=dispatch_uncertain,
            verify_readback=verify_ok,
            allow_readback_recovery=True,
        )
        self.assertTrue(res.content["verified"])
        self.assertEqual(dispatch_calls["n"], 1)

        # 2. Unmentioned field drift during readback raises BusinessMutationIndeterminate
        port = _FakeConventionPort({"id": 1, "name": "Alpha", "note": "Keep", "updatedAt": 100})
        handler = ConventionUpdateHandler(port)
        spec = handler.prepare(
            {"entity": "items", "target": "Alpha", "changes": [{"field": "name", "value": "Alpha2"}]}
        )
        orig_dispatch = port.dispatch_update

        def mutates_unmentioned(s, rid, payload):
            orig_dispatch(s, rid, payload)
            port.record["note"] = "Unexpectedly changed"

        port.dispatch_update = mutates_unmentioned
        with self.assertRaises(BusinessMutationIndeterminate) as caught:
            handler.dispatch(spec.args)
        self.assertEqual(caught.exception.phase, "postcondition")
        self.assertEqual(caught.exception.reconciliation["unverified_effect"], "unrelated_field_preservation")
        self.assertEqual(port.dispatches, 1)

    def test_submit_pipeline_enforces_single_use_and_reread_drift_abort(self):
        with tempfile.TemporaryDirectory() as tmp:
            context, _ledger, approvals = self._approvals(tmp)
            proposal = BusinessMutationProposal(
                tool="native_form_update",
                args={"mode": "update", "record_id": "1"},
                effect="Update form",
                targets=("Item #1",),
                nonce="4" * 32,
                operation_context_digest=context.binding_digest,
                details=(change_detail("Name", "Alpha", "Beta"),),
            )
            pipeline = WritePipeline()
            dispatched = {"count": 0}

            # 1. Pre-dispatch reread drift aborts before dispatch_once
            drift_res = pipeline.run_submit_pipeline(
                proposal=proposal,
                approvals=approvals,
                step=1,
                context=context,
                identity={},
                on_not_approved=lambda _a: "not_approved",
                pre_dispatch_reread=lambda: (_ for _ in ()).throw(
                    BusinessMutationRefusedError("drifted", reason="precondition_changed")
                ),
                on_reread_drift=lambda exc: ("drifted", str(exc)),
                dispatch_once=lambda _ap, _rs: (dispatched.__setitem__("count", dispatched["count"] + 1) or (False, None)),
                settle_and_readback=lambda _ap, _u, _e: "settled",
            )
            self.assertEqual(drift_res, ("drifted", "drifted"))
            self.assertEqual(dispatched["count"], 0)

            # 2. Same proposal cannot be re-entered after consumption
            with self.assertRaises(BusinessMutationBlockedError):
                pipeline.run_submit_pipeline(
                    proposal=proposal,
                    approvals=approvals,
                    step=2,
                    context=context,
                    identity={},
                    on_not_approved=lambda _a: "not_approved",
                    pre_dispatch_reread=lambda: None,
                    on_reread_drift=lambda exc: ("drifted", str(exc)),
                    dispatch_once=lambda _ap, _rs: (False, None),
                    settle_and_readback=lambda _ap, _u, _e: "settled",
                )
            self.assertEqual(dispatched["count"], 0)


if __name__ == "__main__":
    unittest.main()
