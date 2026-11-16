"""A combined form phase cannot complete from partial or stale field readback."""
from dataclasses import replace
import json
from types import SimpleNamespace as NS
import unittest

from web_operation_agent.execution.draft_handoff import add_draft_lines, field_identity, handoff_draft
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

    def test_an_unrestorable_form_never_gets_its_submit_marked(self):
        from web_operation_agent.execution.draft_handoff import unrestorable_form
        record = {"scope": "form:entry", "instance": "live", "lifetime": "L1",
                  "effects": ["结算账户：998036→998031"], "detail": ""}
        blocked = {("form:entry", "live"): record}
        self.assertIs(unrestorable_form(blocked, "form:entry", ("live",)), record)
        self.assertIs(unrestorable_form(blocked, "form:entry", ("L1",)), record)
        self.assertIs(unrestorable_form(blocked, "form:entry", ()), record)
        self.assertIsNone(unrestorable_form(blocked, "form:entry", ("other",)))
        self.assertIsNone(unrestorable_form(blocked, "other-scope", ("live",)))
        before = page()
        context = NS(obs=page(2, first="Synthetic"), audit=lambda *a, **k: None)
        context.page = NS(checkpoint=lambda: None,
                          handoff=lambda *_: self.fail("a blocked form was handed over"))
        result = handoff_draft(context, before,
                               [(field_identity(before.elements[0]), "Synthetic")],
                               "Save", blocked=blocked)
        self.assertEqual((result.state, result.error_code), ("refused", "opening_changes"))
        self.assertIn("结算账户：998036→998031", result.feedback)

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

    def test_tall_dialog_scrolls_to_submit_after_full_field_readback(self):
        attrs = {"form_scope": "dialog:editor", "form_instance": "live"}
        def observation(epoch, *, footer=False, value="Synthetic", instance="live"):
            bound = {**attrs, "form_instance": instance}
            fields = [ElementRef("e1", "textbox", "Name", "input", epoch,
                                 {**bound, "field_label": "Name", "value": value}),
                      ElementRef("e2", "textbox", "Note", "input", epoch,
                                 {**bound, "field_label": "Note", "value": "keep"})]
            if footer:
                fields = [fields[1], ElementRef("e3", "button", "Save", "button", epoch, bound)]
            return PageObservation(1, epoch, "http://app.test/new", "New", [], fields,
                                   "form", page_route="/new", render_state="ready")
        before, filled, footer = observation(1, value=""), observation(2), observation(3, footer=True)
        context = NS(obs=filled, terminal_reason="", audit=lambda *a, **k: None)
        calls = []
        def scroll(action, args):
            calls.append((action, args))
            context.obs = footer
            receipt = ActionReceipt(1, "scroll", "scroll", args, True,
                                    observation=footer, url_before=filled.url, url_after=filled.url)
            return PageActionOutcome("completed", "acknowledged", receipt_json=json.dumps(receipt.to_json()))
        def handoff(fields, submit):
            calls.append(("handoff", fields, submit))
            context.obs = observation(4, footer=True)
            receipt = ActionReceipt(1, "ready", "handoff_form", {}, True,
                                    observation=context.obs, url_before=filled.url, url_after=filled.url)
            return PageActionOutcome("completed", "acknowledged", receipt_json=json.dumps(receipt.to_json()))
        context.page = NS(checkpoint=lambda: None, execute=scroll, handoff=handoff)
        result = handoff_draft(context, before, [(field_identity(before.elements[0]), "Synthetic")], "Save")
        self.assertEqual(result.completion_command, "ready")
        self.assertEqual(calls[0], ("scroll", {"direction": "down", "pages": 1, "ref": "e1"}))
        self.assertNotIn("ref", calls[1][1][0])
        self.assertEqual(calls[1][2]["ref"], "e3")

        context.obs = filled
        wrong = observation(3, footer=True, instance="replaced")
        context.page.execute = lambda action, args: (setattr(context, "obs", wrong)
            or PageActionOutcome("completed", "acknowledged", receipt_json=json.dumps(
                ActionReceipt(1, "scroll", "scroll", args, True, observation=wrong,
                              url_before=filled.url, url_after=filled.url).to_json())))
        result = handoff_draft(context, before, [(field_identity(before.elements[0]), "Synthetic")], "Save")
        self.assertEqual(result.state, "unverified")

    def test_scrolled_footer_baseline_is_the_scrolled_view_and_handoff_may_not_change_it(self):
        attrs = {"form_scope": "dialog:editor", "form_instance": "live"}
        def observation(epoch, *, footer=False, value="Synthetic", extra=False):
            name = ElementRef("e1", "textbox", "Name", "input", epoch, {**attrs, "field_label": "Name", "value": value})
            range_start = ElementRef("e4", "spinbutton", "", "input", epoch,
                                     {**attrs, "id": "synthetic-range-start", "value": "0"})
            fields = [name, range_start]
            if footer:
                fields = [ElementRef("e3", "button", "Sa ve", "button", epoch, attrs),
                          ElementRef("e8", "spinbutton", "", "input", epoch,
                                     {**attrs, "id": "synthetic-lower", "value": "7"})]
                if extra:
                    fields.append(ElementRef("e9", "spinbutton", "", "input", epoch,
                                             {**attrs, "id": "synthetic-new", "value": "1"}))
            return PageObservation(1, epoch, "http://app.test/new", "New", [], fields,
                                   "form", page_route="/new", render_state="ready")
        for extra, expected in ((False, "completed"), (True, "unverified")):
            with self.subTest(extra=extra):
                before, filled = observation(1, value=""), observation(2)
                reasons = []
                context = NS(obs=filled, terminal_reason="",
                             audit=lambda event, **kw: reasons.append(kw.get("reason")))
                def scroll(action, args):
                    context.obs = observation(3, footer=True)
                    receipt = ActionReceipt(1, "scroll", "scroll", args, True, observation=context.obs,
                                            url_before=filled.url, url_after=filled.url)
                    return PageActionOutcome("completed", "acknowledged", receipt_json=json.dumps(receipt.to_json()))
                def handoff(fields, submit):
                    context.obs = observation(4, footer=True, extra=extra)
                    receipt = ActionReceipt(1, "ready", "handoff_form", {}, True, observation=context.obs,
                                            url_before=filled.url, url_after=filled.url)
                    return PageActionOutcome("completed", "acknowledged", receipt_json=json.dumps(receipt.to_json()))
                context.page = NS(checkpoint=lambda: None, execute=scroll, handoff=handoff)
                result = handoff_draft(context, before, [(field_identity(before.elements[0]), "Synthetic")], "Save")
                self.assertEqual(result.state, expected)
                if extra:
                    self.assertIn("final_untouched_set", reasons)

        # Even after scrolling, adding a labelled field during handoff is refused.
        before, filled = observation(1, value=""), observation(2)
        reasons = []
        context = NS(obs=filled, terminal_reason="", audit=lambda event, **kw: reasons.append(kw.get("reason")))
        context.page = NS(
            checkpoint=lambda: None,
            execute=lambda action, args: (
                setattr(context, "obs", observation(3, footer=True))
                or PageActionOutcome("completed", "acknowledged", receipt_json=json.dumps(
                    ActionReceipt(1, "scroll", "scroll", args, True, observation=context.obs,
                                  url_before=filled.url, url_after=filled.url).to_json()))
            ),
            handoff=lambda fields, submit: (
                setattr(context, "obs", replace(observation(4, footer=True), elements=[
                    *observation(4, footer=True).elements,
                    ElementRef("e10", "textbox", "Added", "input", 4, {**attrs, "field_label": "Added", "value": ""}),
                ]))
                or PageActionOutcome("completed", "acknowledged", receipt_json=json.dumps(
                    ActionReceipt(1, "ready", "handoff_form", {}, True, observation=context.obs,
                                  url_before=filled.url, url_after=filled.url).to_json()))
            ),
        )
        refused = handoff_draft(context, before, [(field_identity(before.elements[0]), "Synthetic")], "Save")
        self.assertEqual(refused.state, "unverified")
        self.assertIn("final_field_set", reasons)

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

    def test_query_completion_requires_the_exact_application_verified_click(self):
        from web_operation_agent.execution.action_admission import ActionAdmission
        obs = page(3)
        executor, state = ModelDrivenTurnExecutor.__new__(ModelDrivenTurnExecutor), NS(obs=obs)
        admission = ActionAdmission.__new__(ActionAdmission)
        admission._verified_query = (obs.url, obs.page_route, "null", admission._query_choices(obs))
        admission._verified_query_command = "query"
        executor._admission, executor._verified_action_commands = admission, dict.fromkeys(("query", "other"))
        receipt = ActionReceipt(1, "query", "click", {}, True, observation=obs,
                                url_after=obs.url, write_guard_held=True)
        executor._tool_receipts = [receipt]
        result = LoopToolResult("filtered", completion_command="query")
        self.assertTrue(executor._completion_proved(state, "fill_fields", {}, result))
        for changed in (replace(receipt, command_id="other"), replace(receipt, write_guard_held=False),
                        replace(receipt, ok=False)):
            executor._tool_receipts = [changed]
            self.assertFalse(executor._completion_proved(state, "fill_fields", {},
                replace(result, completion_command=changed.command_id)))
        executor._tool_receipts = [receipt]
        state.obs = replace(obs, page_route="/different")
        self.assertFalse(executor._completion_proved(state, "fill_fields", {}, result))

    def test_addition_allows_new_instance_but_refuses_old_value_or_lifetime_changes(self):
        for change in ("none", "old_value", "lifetime", "no_growth", "refused"):
            with self.subTest(change=change):
                before = page()
                before.elements = [replace(e, attrs={**e.attrs, "form_lifetime": "form-root"}) for e in before.elements]
                before.elements[-1] = replace(before.elements[-1], name="Add line")
                context = NS(obs=before)
                calls = []
                def execute(action, args):
                    calls.append((action, args))
                    if change == "refused":
                        return PageActionOutcome("refused", "not_dispatched", "unreviewed")
                    attrs = {"form_scope": "form:entry", "form_instance": "grown", "form_lifetime": "form-root"}
                    fields = [replace(e, epoch=2, attrs={**e.attrs, **attrs}) for e in before.elements]
                    if change != "no_growth":
                        fields.append(ElementRef("e4", "textbox", "Line", "input", 2,
                                                 {**attrs, "field_label": "Local line 1", "value": ""}))
                    if change == "old_value": fields[1].attrs["value"] = "lost previous draft"
                    if change == "lifetime": fields[0].attrs["form_lifetime"] = "replacement"
                    context.obs = replace(before, epoch=2, elements=fields)
                    receipt = ActionReceipt(1, "addition", action, args, True, observation=context.obs)
                    return PageActionOutcome("completed", "acknowledged", receipt_json=json.dumps(receipt.to_json()))
                context.page = NS(checkpoint=lambda: None, execute=execute)
                result = add_draft_lines(context, "Add line", 1)
                self.assertEqual(1, len(calls))
                self.assertEqual(change == "none", result is None)


class AnonymousUntouchedHandoffTests(unittest.TestCase):
    def run_handoff(self, change="none", *, final_change="none"):
        from web_operation_agent.tests.transport.submit_wire import SubmitWire
        def anonymous(observation):
            attrs = {"form_scope": "form:entry", "form_instance": "live"}
            return replace(observation, elements=[*observation.elements,
                ElementRef("e4", "spinbutton", "", "input", observation.epoch,
                           {**attrs, "id": "synthetic-range-start", "value": "0"}),
                ElementRef("e5", "spinbutton", "", "input", observation.epoch,
                           {**attrs, "id": "synthetic-range-end", "value": "30"})])
        before, after = anonymous(page()), anonymous(page(2, first="Synthetic"))
        if change in {"missing_id", "duplicate_id"}:
            identifier = "" if change == "missing_id" else "synthetic-range-start"
            for observation in (before, after):
                observation.elements[-1] = replace(observation.elements[-1],
                    attrs={**observation.elements[-1].attrs, "id": identifier})
        if change in {"replaced_id", "changed_value", "changed_tag"}:
            target = after.elements[-1]
            after.elements[-1] = (replace(target, tag="textarea") if change == "changed_tag" else
                replace(target, attrs={**target.attrs,
                    **({"id": "replacement"} if change == "replaced_id" else {"value": "31"})}))
        if change == "replaced_instance":
            after = page(2, first="Synthetic", instance="replacement")
        if change == "reindexed":
            after.elements[-2:] = [replace(e, ref=ref) for e, ref in zip(after.elements[-2:], ("e44", "e48"))]
        wire = SubmitWire(before)
        before = wire.channel.wait_for_observation(timeout_s=0).observation
        issued = wire.command("fill", {"ref": "e1", "value": "Synthetic"})
        received = wire.result(issued, ActionReceipt(1, issued.action_id, "fill", issued.command.args,
            True, observation=after, url_before=before.url, url_after=after.url))
        if change == "anonymous_filled":
            # Even an unchanged value entered through a real action cannot promote an
            # anonymous comparison id into an authorized field binding.
            issued = wire.command("fill", {"ref": "e5", "value": "30"})
            entered = replace(received.observation, epoch=received.observation.epoch + 1,
                elements=[replace(e, epoch=received.observation.epoch + 1) for e in received.observation.elements])
            received = wire.result(issued, ActionReceipt(1, issued.action_id, "fill", issued.command.args,
                True, observation=entered, url_before=entered.url, url_after=entered.url))
        context = NS(obs=received.observation, audit=lambda *_a, **_kw: None)
        calls = []
        def dispatch(fields, submit):
            calls.append((fields, submit))
            issued = wire.command("handoff_form", {"fields": fields, "submit": submit})
            final = replace(context.obs, epoch=context.obs.epoch + 1,
                elements=[replace(e, epoch=context.obs.epoch + 1) for e in context.obs.elements])
            if final_change == "missing": final.elements.pop()
            if final_change == "duplicate":
                final.elements[-1] = replace(final.elements[-1], attrs={**final.elements[-1].attrs,
                    "id": final.elements[-2].attrs["id"]})
            if final_change == "value":
                final.elements[-1] = replace(final.elements[-1], attrs={**final.elements[-1].attrs, "value": "31"})
            receipt = wire.result(issued, ActionReceipt(1, issued.action_id, "handoff_form", issued.command.args,
                True, observation=final, url_before=context.obs.url, url_after=final.url))
            context.obs = receipt.observation
            return PageActionOutcome("completed", "acknowledged", receipt_json=json.dumps(receipt.to_json()))
        context.page = NS(checkpoint=lambda: None, handoff=dispatch)
        wanted = [(field_identity(before.elements[0]), "Synthetic")]
        if change == "anonymous_filled": wanted.append((field_identity(before.elements[-1]), "30"))
        result = handoff_draft(context, before, wanted, "Save")
        return result, calls, wire

    def test_unique_dom_ids_only_compare_unchanged_anonymous_fields(self):
        for change in ("none", "reindexed"):
            with self.subTest(change=change):
                result, calls, wire = self.run_handoff(change)
                self.assertEqual((result.state, result.marked), ("completed", "Save"))
                self.assertEqual(len(calls), 1)
                fields = calls[0][0]
                self.assertEqual([field["field"] for field in fields], ["name"])
                self.assertNotIn("synthetic-range", json.dumps(wire.commands[-1].args))
                self.assertEqual(len(field_identity(page().elements[0])), 6)

    def test_anonymous_fills_missing_duplicate_replaced_or_changed_controls_are_refused(self):
        for change in ("anonymous_filled", "missing_id", "duplicate_id", "replaced_id",
                       "changed_value", "changed_tag", "replaced_instance"):
            with self.subTest(change=change):
                result, calls, wire = self.run_handoff(change)
                self.assertEqual(result.state, "unverified")
                self.assertFalse(result.marked)
                self.assertEqual(calls, [])
                self.assertNotIn("handoff_form", [command.action for command in wire.commands])

    def test_final_readback_still_rejects_missing_duplicate_or_changed_anonymous_controls(self):
        for change in ("missing", "duplicate", "value"):
            with self.subTest(change=change):
                result, _, _ = self.run_handoff(final_change=change)
                self.assertEqual(result.state, "unverified")
                self.assertFalse(result.marked)
