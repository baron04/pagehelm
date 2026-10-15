"""A combined form phase cannot complete from partial or stale field readback."""
from dataclasses import replace
import json
from types import SimpleNamespace as NS
import unittest

from web_operation_agent.execution.draft_handoff import field_identity, handoff_draft
from web_operation_agent.execution.model_loop import ModelDrivenTurnExecutor
from web_operation_agent.execution.model_loop_contracts import LoopToolResult, PageActionOutcome
from web_operation_agent.schemas import ActionReceipt, ElementRef, PageObservation


def page(epoch=1, first="", other="keep", instance="live"):
    attrs = {"form_scope": "form:entry", "form_instance": instance}
    return PageObservation(1, epoch, "http://app.test/new", "New", [], [
        ElementRef("e1", "textbox", "Name", "input", epoch, {**attrs, "field": "name", "value": first}),
        ElementRef("e2", "textbox", "Note", "input", epoch, {**attrs, "field": "note", "value": other}),
        ElementRef("e3", "button", "Save", "button", epoch, attrs)], "form", page_route="/new", render_state="ready")


class DraftHandoffTests(unittest.TestCase):
    def handoff(self, after, *, final=None):
        before, calls = page(), []
        context = NS(obs=after, audit=lambda *a, **k: None)
        def dispatch(fields, submit):
            calls.append((fields, submit))
            context.obs = final or replace(after, epoch=after.epoch + 1,
                elements=[replace(e, epoch=after.epoch + 1) for e in after.elements])
            receipt = ActionReceipt(1, "actual-handoff", "handoff_form", {}, True,
                observation=context.obs, url_before=after.url, url_after=after.url)
            return PageActionOutcome("completed", "acknowledged", receipt_json=json.dumps(receipt.to_json()))
        context.page = NS(checkpoint=lambda: None, handoff=dispatch)
        result = handoff_draft(context, before, [(field_identity(before.elements[0]), "Synthetic")], "Save")
        return result, calls

    def test_verified_complete_phase_carries_actual_receipt_proof(self):
        result, calls = self.handoff(page(2, first="Synthetic"))
        self.assertEqual("completed", result.state)
        self.assertEqual("actual-handoff", result.completion_command)
        self.assertEqual("Save", result.marked)
        self.assertEqual(1, len(calls))

    def test_missing_value_replaced_form_or_untouched_change_never_hands_off(self):
        for after in (page(2), page(2, first="Synthetic", instance="replaced"),
                      page(2, first="Synthetic", other="unexpected")):
            with self.subTest(after=after):
                result, calls = self.handoff(after)
                self.assertEqual("unverified", result.state)
                self.assertFalse(result.completion_command)
                self.assertEqual([], calls)

    def test_untouched_change_during_runtime_handoff_cannot_complete(self):
        result, _ = self.handoff(page(2, first="Synthetic"), final=page(3, first="Synthetic", other="changed"))
        self.assertEqual("unverified", result.state)
        self.assertFalse(result.completion_command)

    def test_adapter_completion_id_must_name_this_tools_actual_fresh_handoff_receipt(self):
        executor, state = ModelDrivenTurnExecutor.__new__(ModelDrivenTurnExecutor), NS(obs=page(3, first="Synthetic"))
        receipt = ActionReceipt(1, "actual", "handoff_form", {}, True, observation=state.obs, url_after=state.obs.url)
        executor._tool_receipts = [receipt]
        result = LoopToolResult("ready", completion_command="actual")
        self.assertTrue(executor._completion_proved(state, "any_application_procedure", {}, result))
        self.assertFalse(executor._completion_proved(state, "any_application_procedure", {}, replace(result, completion_command="invented")))
        executor._tool_receipts = [replace(receipt, action="highlight")]
        self.assertFalse(executor._completion_proved(state, "any_application_procedure", {}, result))
        executor._tool_receipts = [replace(receipt, observation=page(2, first="Synthetic"))]
        self.assertFalse(executor._completion_proved(state, "any_application_procedure", {}, result))
