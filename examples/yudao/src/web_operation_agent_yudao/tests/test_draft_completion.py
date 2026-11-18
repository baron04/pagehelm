"""A completed entry batch cannot stand in for the live draft handoff."""
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from web_operation_agent.execution.model_loop import ModelDrivenTurnExecutor
from web_operation_agent.execution.model_loop_contracts import LoopToolResult, PageActionOutcome
from web_operation_agent.schemas import ActionReceipt, ElementRef, PageObservation
from web_operation_agent.privacy import BASELINE_SESSION_HANDOFFS
from web_operation_agent.tests.execution.test_product_model_loop import LoopFixture, call, reply
from web_operation_agent_yudao.executor.page_prompts import SlimTurnContext
from web_operation_agent_yudao.executor.slim_loop import (
    SlimTurnExecutor, TableReadTurnExecutor, YudaoLoopProcedures)
from web_operation_agent_yudao.reads.turn_tables import TurnTables
from web_operation_agent_yudao.writes.write_intent import WriteIntent


def form(*, lifetime="live-a", instance="fields-a", query=False):
    attrs = {"form_scope": "form:entry", "form_instance": instance,
             "form_lifetime": lifetime, **({"page_control": "query"} if query else {})}
    elements = [ElementRef(f"e{i}", "textbox", label, "input", 1,
        {**attrs, "field_label": label, "field": label, "value": value})
        for i, (label, value) in enumerate((("名称", ""), ("备注", ""), ("默认值", "native")), 1)]
    elements.append(ElementRef("e4", "button", "Review", "button", 1, attrs))
    return PageObservation(1, 1, "https://synthetic.test/entry", "Entry", [], elements,
        content="\n".join(f"[{i}]<{'input' if i < 4 else 'button'} />" for i in range(1, 5)),
        render_state="ready", page_route="/entry")


class DraftLoop(LoopFixture, SlimTurnExecutor):
    """The real decision loop and field/readback procedures over an isolated transport."""

    def __init__(self, replies, *, query=False, handoff_ok=True, reindex_handoff=False, drop_scope_after_fill=False):
        super().__init__(replies, lambda *_: None)
        self.state.obs = form(query=query)
        self.state.audit = self.state.ledger.append
        self.handoff_ok = handoff_ok
        self.reindex_handoff = reindex_handoff
        self.drop_scope_after_fill = drop_scope_after_fill
        self._tables = TurnTables(BASELINE_SESSION_HANDOFFS)
        self.turn_context = SlimTurnContext(
            current_request="名称先填Synthetic，备注填Note，默认值不变，由我提交并标出按钮",
            write_intent=WriteIntent("draft"),
            turn_tables=(self._tables, "2026-11-14", "Asia/Shanghai"),
        )
        self.owner = object.__new__(YudaoLoopProcedures)
        self.owner.turn_context = self.turn_context
        self.owner.goal_port = NS(data=None)
        self.owner.knowledge = None
        self.owner._settled = lambda *_args, **_kwargs: None
        self.state.page = NS(checkpoint=lambda: None,
            execute=self.execute_page,
            handoff=lambda fields, submit: self.execute_page("handoff_form", {"fields": fields, "submit": submit}))

    def _dispatch(self, state, action, args, **_kwargs):
        from web_operation_agent.tests.transport.submit_wire import SubmitWire
        if not hasattr(self, "wire"):
            self.wire = SubmitWire(state.obs)
        issued = self.wire.command(action, args)
        receipt = self._browser(state, action, issued.command.args)
        returned = self.wire.result(issued, receipt)
        self._tool_receipts[-1] = returned
        return returned

    def _browser(self, state, action, args):
        before = state.obs
        epoch = max(before.epoch, self.wire.current.page_epoch) + 1
        elements = [replace(e, epoch=epoch) for e in before.elements]
        if action in {"fill", "select"}:
            value = {"value": args["value"], **({"selected_option": args["option_label"]} if action == "select" else {})}
            elements = [replace(e, attrs={**e.attrs, **value})
                        if e.ref == args["ref"] else e for e in elements]
            if self.drop_scope_after_fill:
                elements = [replace(e, attrs={key: value for key, value in e.attrs.items()
                                             if key != "form_scope"}) for e in elements]
        if action == "handoff_form" and self.reindex_handoff:
            elements = [replace(e, ref=f"e{i}") for i, e in enumerate(elements, 11)]
        state.obs = replace(before, epoch=epoch, elements=elements)
        receipt = ActionReceipt(1, f"command-{len(self.actions)}", action, args,
            action != "handoff_form" or self.handoff_ok,
            observation=state.obs, url_before=before.url, url_after=state.obs.url,
            error_code="" if action != "handoff_form" or self.handoff_ok else "stale_observation")
        self._tool_receipts.append(receipt)
        self.actions.append(action)
        return receipt

    def execute_page(self, action, args, **kwargs):
        receipt = self._dispatch(self.state, action, args, **kwargs)
        return PageActionOutcome("completed" if receipt.ok else "unverified", "acknowledged",
            receipt.error_code, receipt_json=json.dumps(receipt.to_json()))

    def procedure(self, _executor, state, name, args, turn):
        local = {"touched": [], "trace": [], "marked": ""}
        if name in {"fill_fields", "filter_page"}:
            result = self.owner._fill_fields(state, args["fields"], "", local,
                then_highlight=args.get("then_highlight", ""), add_lines=args.get("add_lines"),
                query_only=name == "filter_page")
        elif name in {"input", "select"}:
            result = self.owner._tool(state, name, args, local)
        elif name == "highlight":
            self._dispatch(state, "highlight", {"ref": "e4"})
            result = LoopToolResult("marked", kind="page", marked="Review")
        elif name == "replace_form":
            state.obs = form(lifetime="live-b", instance="fields-b")
            result = LoopToolResult("replaced", kind="page")
        elif name == "grow_form":
            state.obs = replace(state.obs, elements=[replace(e, attrs={**e.attrs,
                "form_instance": "grown-fields"}) for e in state.obs.elements])
            result = LoopToolResult("grown", kind="page")
        else:
            raise AssertionError(name)
        return replace(result, entered=tuple(local["touched"]), trace=tuple(local["trace"]))

    def run(self):
        try:
            return super().run()
        finally:
            self._tables.close()

    def _invoke_tool(self, state, name, args, turn):
        # Replace only tool transport/admission; exercise the production adapter's
        # tracking, real form entry/readback, and the common finish/shortcut paths.
        with patch.object(ModelDrivenTurnExecutor, "_invoke_tool",
                          lambda executor, state, name, args, turn: self.procedure(executor, state, name, args, turn)):
            return SlimTurnExecutor._invoke_tool(self, state, name, args, turn)


def fill(label="名称", value="Synthetic", **kwargs):
    return reply(call("fill_fields", fields=[{"label": label, "value": value}], **kwargs))


def finish(status="done"):
    return reply(call("finish", status=status, answer="Synthetic，备注为空，默认值保持原值。", items=[
        {"item": "表单预填", "kind": "page",
         "status": "delivered" if status == "done" else "undelivered", "receipts": []}]))


class DraftCompletionTests(unittest.TestCase):
    def test_fields_only_then_done_is_returned_once_and_cannot_claim_completion(self):
        loop = DraftLoop([fill(), finish(), finish()])
        answer = loop.run()
        self.assertEqual(loop.state.terminal_reason, "agent_refused")
        self.assertIn("尚未完成同一实时表单", answer)
        self.assertEqual(loop.actions, ["fill"])
        rejected = [facts for event, facts in loop.events if event == "model_delivery_rejected"]
        self.assertEqual([r["returned"] for r in rejected], [True, False])
        self.assertEqual(loop.state.obs.elements[1].attrs["value"], "")
        self.assertEqual(loop.state.obs.elements[2].attrs["value"], "native")

    def test_last_batch_can_complete_real_handoff_after_an_earlier_open_batch(self):
        loop = DraftLoop([fill(), fill("备注", "Note", then_highlight="Review", answer="已预填")])
        answer = loop.run()
        self.assertEqual(loop.state.terminal_reason, "")
        self.assertEqual(loop.actions, ["fill", "fill", "handoff_form"])
        self.assertIn("手动点击「Review」提交", answer)
        self.assertEqual(loop.state.obs.elements[0].attrs["value"], "Synthetic")

    def test_missing_handoff_can_be_corrected_once_with_the_real_handoff(self):
        loop = DraftLoop([fill(), finish(), fill(then_highlight="Review", answer="已预填")])
        self.assertIn("手动点击「Review」提交", loop.run())
        self.assertEqual(loop.actions, ["fill", "fill", "handoff_form"])
        self.assertNotIn("pending_delivery", loop.turn)

    def test_query_fields_and_unfilled_location_highlight_keep_their_existing_completion(self):
        loop = DraftLoop([fill(), finish()], query=True)
        self.assertNotIn("尚未完成同一实时表单", loop.run())
        self.assertFalse(loop.turn.get("draft_form_entries"))
        loop = DraftLoop([
            reply(call("highlight", index=4)),
            reply(call("finish", status="done", answer="已标出", items=[
                {"item": "Review按钮", "kind": "page", "status": "delivered", "receipts": []}])),
        ])
        self.assertEqual(loop.run(), "已标出")

    def test_query_input_and_filter_with_a_same_name_business_field_do_not_require_handoff(self):
        for action in (reply(call("input", index=1, text="Synthetic")),
                       reply(call("filter_page", fields=[{"label": "名称", "value": "Synthetic"}]))):
            with self.subTest(action=action):
                loop = DraftLoop([action, finish()], query=True)
                other = replace(loop.state.obs.elements[0], ref="e9", attrs={
                    **loop.state.obs.elements[0].attrs, "form_scope": "form:unrelated", "page_control": "form"})
                loop.state.obs = replace(loop.state.obs, elements=[*loop.state.obs.elements, other],
                                         content=loop.state.obs.content + "\n[9]<input />")
                self.assertNotIn("尚未完成同一实时表单", loop.run())
                self.assertFalse(loop.turn.get("draft_form_entries"))
                self.assertEqual(loop.state.obs.elements[-1].attrs["value"], "")

    def test_business_input_uses_its_actual_target_and_keeps_the_handoff_obligation(self):
        loop = DraftLoop([reply(call("input", index=1, text="Synthetic")), finish(), finish()])
        self.assertIn("尚未完成同一实时表单", loop.run())
        self.assertEqual(loop.state.terminal_reason, "agent_refused")

    def test_select_receipt_tracks_the_actual_business_or_query_control(self):
        for query in (False, True):
            with self.subTest(query=query):
                loop = DraftLoop([reply(call("select", index=1, option="Synthetic")), finish(), finish()], query=query)
                loop.state.obs.elements[0] = replace(loop.state.obs.elements[0], role="combobox")
                answer = loop.run()
                self.assertEqual(bool(loop.turn.get("draft_form_entries")), not query)
                self.assertEqual("尚未完成同一实时表单" in answer, not query)
                self.assertEqual(loop.actions, ["select"])

    def test_query_batch_ignores_an_unshown_business_field_with_the_same_name(self):
        loop = DraftLoop([fill(), finish()], query=True)
        other = replace(loop.state.obs.elements[0], ref="e9", attrs={
            **loop.state.obs.elements[0].attrs, "form_scope": "form:unrelated", "page_control": "form"})
        loop.state.obs = replace(loop.state.obs, elements=[*loop.state.obs.elements, other])
        self.assertNotIn("尚未完成同一实时表单", loop.run())
        self.assertFalse(loop.turn.get("draft_form_entries"))
        self.assertEqual(loop.state.obs.elements[-1].attrs["value"], "")

    def test_ordinary_highlight_answer_cannot_bypass_pending_form_handoff(self):
        loop = DraftLoop([fill(), reply(call("highlight", index=4, answer="全部完成")), finish("cannot")])
        self.assertNotEqual(loop.run(), "全部完成")
        self.assertEqual(loop.state.terminal_reason, "agent_refused")
        self.assertIn("这一步带的 answer 没有用上", str(loop._messages))
        self.assertNotIn("handoff_form", loop.actions)

    def test_repeated_shortcut_uses_the_same_bounded_delivery_correction(self):
        shortcut = fill(then_highlight="Review", answer="全部完成")
        loop = DraftLoop([fill(), reply(call("replace_form")), shortcut, shortcut])
        self.assertIn("尚未完成同一实时表单", loop.run())
        self.assertEqual(loop.state.terminal_reason, "agent_refused")
        rejected = [facts for event, facts in loop.events if event == "model_delivery_rejected"]
        self.assertEqual([r["returned"] for r in rejected], [True, False])
        self.assertEqual(loop.actions, ["fill", "observe", "fill", "handoff_form", "fill", "handoff_form"])

    def test_another_live_form_handoff_does_not_discharge_the_original_form(self):
        loop = DraftLoop([fill(), reply(call("replace_form")), fill(then_highlight="Review"), finish(), finish()])
        self.assertIn("尚未完成同一实时表单", loop.run())
        self.assertEqual(loop.state.terminal_reason, "agent_refused")
        self.assertEqual(loop.actions.count("handoff_form"), 1)

    def test_unverified_handoff_and_stale_current_page_cannot_unlock_done(self):
        loop = DraftLoop([fill(then_highlight="Review"), finish(), finish()], handoff_ok=False)
        self.assertIn("尚未完成同一实时表单", loop.run())
        self.assertEqual(loop.state.terminal_reason, "agent_refused")
        loop = DraftLoop([fill(then_highlight="Review"), reply(call("replace_form")), finish(), finish()])
        self.assertIn("尚未完成同一实时表单", loop.run())

    def test_table_delivery_with_a_current_page_receipt_still_needs_the_draft_handoff(self):
        loop = DraftLoop([fill(), finish("cannot")])
        loop.run()
        executor = object.__new__(TableReadTurnExecutor)
        tables = TurnTables(BASELINE_SESSION_HANDOFFS)
        self.addCleanup(tables.close)
        executor.turn_context = SlimTurnContext(turn_tables=(tables, "2026-11-14", "Asia/Shanghai"))
        reference = tables.issue_receipt()
        loop.turn["page_delivery_receipts"] = {
            reference: hashlib.sha256(executor._page_fingerprint(loop.state.obs).encode()).hexdigest()}
        args = {"status": "done", "answer": "完成", "items": [{
            "item": "预填", "kind": "page", "status": "delivered", "receipts": [reference]}]}
        feedback = executor._finish_feedback(loop.state, loop.turn, args)
        self.assertEqual(feedback.code, "draft_handoff_required")
        self.assertEqual(executor._finish_feedback(loop.state, {**loop.turn, "draft_form_entries": set()}, args), "")

    def test_successful_handoff_rebinds_after_observation_refs_change(self):
        loop = DraftLoop([fill(then_highlight="Review", answer="已预填")], reindex_handoff=True)
        self.assertIn("手动点击「Review」提交", loop.run())
        self.assertEqual(loop.actions, ["fill", "handoff_form"])
        self.assertEqual(loop.state.obs.elements[-1].ref, "e14")

    def test_same_form_growth_between_batches_uses_lifetime_continuity(self):
        loop = DraftLoop([fill(), reply(call("grow_form")),
                          fill("备注", "Note", then_highlight="Review", answer="已预填")])
        self.assertIn("手动点击「Review」提交", loop.run())
        self.assertEqual(loop.actions, ["fill", "observe", "fill", "handoff_form"])

    def test_full_length_field_label_cannot_evade_the_handoff_requirement(self):
        label = "名称" * 30
        loop = DraftLoop([fill(label), finish(), finish()])
        loop.state.obs.elements[0] = replace(loop.state.obs.elements[0], name=label,
            attrs={**loop.state.obs.elements[0].attrs, "field_label": label, "field": label})
        self.assertIn("尚未完成同一实时表单", loop.run())
        self.assertEqual(loop.state.terminal_reason, "agent_refused")

    def test_missing_or_malformed_form_identity_fails_closed_after_an_entered_result(self):
        for identity in (None, "", [], {"invalid": True}, 42):
            with self.subTest(identity=identity):
                loop = DraftLoop([fill(), finish(), finish()])
                loop.state.obs = replace(loop.state.obs, elements=[replace(e, attrs={
                    **e.attrs, "form_lifetime": identity, "form_instance": identity}) for e in loop.state.obs.elements])
                self.assertIn("尚未完成同一实时表单", loop.run())
                self.assertEqual(loop.state.terminal_reason, "agent_refused")

    def test_a_form_that_loses_scope_after_entry_cannot_erase_its_handoff_obligation(self):
        loop = DraftLoop([fill(), finish(), finish()], drop_scope_after_fill=True)
        self.assertIn("尚未完成同一实时表单", loop.run())
        self.assertEqual(loop.state.terminal_reason, "agent_refused")


if __name__ == "__main__":
    unittest.main()
