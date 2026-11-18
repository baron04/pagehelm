"""A form filled and then ended without a card or a handoff still gets its submit control marked."""
from dataclasses import replace
from types import SimpleNamespace as NS
import unittest

from web_operation_agent.execution.model_loop import ModelDrivenTurnExecutor
from web_operation_agent_yudao.executor.page_prompts import SlimTurnContext
from web_operation_agent_yudao.tests.test_draft_completion import DraftLoop, fill, finish, form
from web_operation_agent_yudao.writes.write_intent import WriteIntent


def saving_form():
    observation = form()
    elements = [replace(e, name="保存") if e.role == "button" else e for e in observation.elements]
    return replace(observation, elements=elements)


class Loop(DraftLoop):
    def __init__(self, replies, **kwargs):
        super().__init__(replies, **kwargs)
        self.state.obs = saving_form()
        self.procedures = self.owner
        self.goal_port = NS(data=None)
        self.owner.form_fill = None


Loop._procedure_context = lambda self, s, name, **kwargs: self.state


class DefaultHandoffTests(unittest.TestCase):
    def intent(self, mode, text):
        self._turn_ctx = SlimTurnContext(current_request=text, write_intent=WriteIntent(mode))

    def _run_loop(self, loop):
        ctx = replace(self._turn_ctx, turn_tables=loop.turn_context.turn_tables)
        loop.turn_context = ctx
        loop.owner.turn_context = ctx
        return loop.run()

    def test_draft_turn_ending_after_fill_gets_the_program_handoff_and_says_not_submitted(self):
        self.intent("draft", "名称先填Synthetic，由我提交")
        loop = Loop([fill(), finish()])
        answer = self._run_loop(loop)
        self.assertEqual(loop.state.terminal_reason, "")
        self.assertEqual(loop.actions, ["fill", "handoff_form"])
        self.assertIn("「保存」已标出", answer)
        self.assertIn("尚未提交", answer)
        self.assertEqual([e for e, _ in loop.events if e == "default_handoff"], ["default_handoff"])
        self.assertTrue(loop.turn["handoff"][1] == "保存")

    def test_a_save_request_that_never_asked_for_a_card_is_handed_over_not_dropped(self):
        self.intent("save", "把名称填成Synthetic并保存")
        loop = Loop([fill(), finish()])
        answer = self._run_loop(loop)
        self.assertEqual(loop.actions, ["fill", "handoff_form"])
        self.assertIn("尚未提交", answer)
        self.assertNotIn("已提交", answer.replace("尚未提交", ""))

    def test_need_input_and_cannot_keep_a_verified_prefill_handoff(self):
        for status in ("need_input", "cannot"):
            with self.subTest(status=status):
                self.intent("save", "名称先填Synthetic，其余等我补充")
                loop = Loop([fill(), finish(status)])
                answer = self._run_loop(loop)
                self.assertEqual(loop.actions, ["fill", "handoff_form"])
                self.assertIn("尚未提交", answer)
                self.assertEqual(loop.turn["handoff"][1], "保存")
                # Re-observation and another unfinished close do not dismiss the handoff.
                loop._closing(loop.state, loop.turn, {"status": status, "answer": answer})
                self.assertEqual(loop.actions, ["fill", "handoff_form"])

    def test_failed_opening_restoration_never_default_marks_save(self):
        self.intent("save", "名称先填Synthetic")
        loop = Loop([fill(), finish("need_input")])
        loop.turn["submit_handoff_blocked"] = "opening_changes"
        self._run_loop(loop)
        self.assertEqual(loop.actions, ["fill"])

    def test_a_successful_native_select_is_a_real_current_turn_value_candidate(self):
        from web_operation_agent.execution.value_source import check_entries
        from web_operation_agent.tests.execution.test_product_model_loop import call, reply
        self.intent("save", "选择现有分类")
        loop = Loop([reply(call("select", index=1, option="Synthetic option")), finish("cannot")])
        loop.state.obs.elements[0] = replace(loop.state.obs.elements[0], role="combobox")
        loop.native_submit = NS()
        loop.capture_form_baseline = lambda *_: None
        self._run_loop(loop)
        verdict = check_entries(loop.turn["source_ledger"], ["选择现有分类"])[0]
        self.assertEqual(verdict.source, "candidate")
        self.assertTrue(verdict.ok)

    def test_transactionally_restored_defaults_do_not_block_conversion_handoff(self):
        self.intent("save", "名称先填Synthetic")
        loop = Loop([fill()])
        self.addCleanup(loop._tables.close)
        ctx = replace(self._turn_ctx, turn_tables=loop.turn_context.turn_tables)
        loop.turn_context = loop.owner.turn_context = ctx
        loop.run = lambda: None
        loop._invoke_tool(loop.state, "fill_fields", {"fields": [{"label": "名称", "value": "Synthetic"}]}, loop.turn)
        old = loop.state.obs
        loop.state.obs = replace(old, elements=[replace(e, attrs={**e.attrs, "value": "restored"})
            if e.attrs.get("field_label") == "默认值" else e for e in old.elements])
        loop.turn["restored_form_labels"] = {"默认值"}
        self.assertEqual(loop._default_handoff(loop.state, loop.turn), "保存")
        self.assertEqual(loop.state.obs.elements[2].attrs["value"], "restored")

    def test_two_possible_submit_controls_are_not_guessed(self):
        self.intent("save", "把名称填成Synthetic并保存")
        loop = Loop([fill(), finish()])
        loop.state.obs = replace(loop.state.obs, elements=[
            *loop.state.obs.elements,
            replace(loop.state.obs.elements[-1], ref="e9", name="确定")])
        answer = self._run_loop(loop)
        self.assertEqual(loop.actions, ["fill"])
        self.assertIn("没能核验并标出提交按钮", answer)

    def test_a_turn_that_started_a_native_submit_is_left_alone(self):
        self.intent("save", "把名称填成Synthetic并保存")
        loop = Loop([fill(), finish()])
        loop.turn["submit_started"] = True
        self._run_loop(loop)
        self.assertEqual(loop.actions, ["fill"])

    def test_the_reply_is_never_replaced(self):
        self.intent("save", "把名称填成Synthetic并保存")
        loop = Loop([fill(), finish()])
        answer = self._run_loop(loop)
        self.assertTrue(answer.startswith("Synthetic，备注为空，默认值保持原值。"))


if __name__ == "__main__":
    unittest.main()
