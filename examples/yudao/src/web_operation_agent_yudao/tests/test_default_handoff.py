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
                self.assertNotIn("「保存」已标出（尚未提交）", answer)
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

    def test_clean_form_handoff_prevents_refused_close_even_with_cannot(self):
        self.intent("save", "帮我建个联系人：陈斌")
        loop = Loop([fill(then_highlight="保存"), finish("cannot")])
        answer = self._run_loop(loop)
        self.assertEqual(loop.state.terminal_reason, "")
        self.assertEqual(loop.actions, ["fill", "handoff_form"])
        self.assertIn("手动点击「保存」提交", answer)

    def test_blocked_or_missing_handoff_with_cannot_stays_refused(self):
        self.intent("save", "帮我建个联系人：陈斌")
        blocked = Loop([fill(), finish("cannot")])
        blocked.turn["submit_handoff_blocked"] = "opening_changes"
        self._run_loop(blocked)
        self.assertEqual(blocked.state.terminal_reason, "agent_refused")

        no_handoff = Loop([finish("cannot")])
        self._run_loop(no_handoff)
        self.assertEqual(no_handoff.state.terminal_reason, "agent_refused")

    def test_request_submit_on_prefilled_form_button_uses_handoff_while_row_button_delegates(self):
        from unittest.mock import patch
        self.intent("save", "帮我建个联系人：陈斌")
        loop = Loop([fill()])
        self.addCleanup(loop._tables.close)
        ctx = replace(self._turn_ctx, turn_tables=loop.turn_context.turn_tables)
        loop.turn_context = loop.owner.turn_context = ctx
        loop.run = lambda: None
        loop.native_submit = NS()
        loop._invoke_tool(loop.state, "fill_fields", {"fields": [{"label": "名称", "value": "Synthetic"}]}, loop.turn)
        with patch.object(ModelDrivenTurnExecutor, "_request_submit", side_effect=AssertionError("must not run form submit pipeline")):
            res = loop._request_submit(loop.state, {"index": 4}, loop.turn)
        self.assertEqual(res.kind, "page")
        self.assertEqual(res.state, "completed")
        self.assertEqual(res.marked, "保存")
        self.assertEqual(loop.turn["handoff"][1], "保存")

        row_btn = replace(loop.state.obs.elements[-1], ref="e8", name="停用", attrs={"row_id": "42"})
        loop.state.obs = replace(loop.state.obs, elements=[*loop.state.obs.elements, row_btn])
        with patch.object(ModelDrivenTurnExecutor, "_request_submit", return_value="delegated") as mock_super:
            out = loop._request_submit(loop.state, {"index": 5}, loop.turn)
        self.assertEqual(out, "delegated")
        mock_super.assert_called_once()

    def test_form_handoff_awaits_press_and_readback_in_turn(self):
        from web_operation_agent_yudao.executor.slim_loop import _handoff_key

        class NS:
            def contract(self):
                from web_operation_agent.execution.submit_contract import SubmitConventions
                return SubmitConventions(
                    volatile_keys=frozenset({"updateTime"}),
                    record_key="id",
                    line_key="id",
                    name_key="name",
                    epoch_zone=None,
                    decode_write=lambda status, body: (status, None),
                )

            def user_write_mark(self):
                return 1

            def __init__(self, event, record=None):
                self._event = event
                self._record = record

            def user_write_event(self, prefixes, record_id, mark, create=False):
                return self._event

            def read_record(self, prefixes, record_id, *, context_params=None):
                return self._record

        # 1. Verified update finishes cleanly without manual-submit boilerplate
        self.intent("save", "把员工邮箱改成 ops@example.com")
        loop = Loop([fill(), finish()])
        hkey = _handoff_key(loop.state.obs, "保存")
        loop.native_submit = NS(
            {"method": "PUT", "path": "/admin-api/system/user/update", "status": 200, "record_id": "5", "accepted": True},
            {"id": 5, "email": "ops@example.com", "updateTime": "2026-11-25 10:00:00"},
        )
        loop.turn["handoff_target_key"] = hkey
        loop.turn["submit_targets"] = {hkey: {
            "kind": "form", "mode": "update", "prefixes": ("/admin-api/system/user/",),
            "record_id": "5", "baseline": {"id": 5, "email": "old@example.com", "updateTime": "2026-11-24"},
        }}
        answer = self._run_loop(loop)
        self.assertEqual(loop.state.terminal_reason, "")
        self.assertIn("email：old@example.com→ops@example.com", answer)
        self.assertNotIn("updateTime", answer)
        self.assertNotIn("尚未提交", answer)
        self.assertNotIn("请核对后，手动点击", answer)

        # 2. Rejected click refuses honestly with redacted reason
        self.intent("save", "把员工邮箱改成 ops@example.com")
        loop_rej = Loop([fill(), finish()])
        hkey_rej = _handoff_key(loop_rej.state.obs, "保存")
        loop_rej.native_submit = NS({
            "method": "PUT", "path": "/admin-api/system/user/update", "status": 200,
            "record_id": "5", "accepted": False, "app_code": 400,
            "error_msg": "校验未通过 password=secret",
        })
        loop_rej.turn["handoff_target_key"] = hkey_rej
        loop_rej.turn["submit_targets"] = {hkey_rej: {
            "kind": "form", "mode": "update", "prefixes": ("/admin-api/system/user/",),
            "record_id": "5", "baseline": {"id": 5, "email": "old@example.com"},
        }}
        ans_rej = self._run_loop(loop_rej)
        self.assertEqual(loop_rej.state.terminal_reason, "agent_refused")
        self.assertIn("应用拒绝了这次保存", ans_rej)
        self.assertIn("[redacted]", ans_rej)
        self.assertNotIn("secret", ans_rej)
        self.assertIn("未保存落库", ans_rej)

        # 3. No press refuses honestly as not persisted
        self.intent("save", "把员工邮箱改成 ops@example.com")
        loop_none = Loop([fill(), finish()])
        hkey_none = _handoff_key(loop_none.state.obs, "保存")
        loop_none.native_submit = NS(None)
        loop_none.turn["handoff_target_key"] = hkey_none
        loop_none.turn["submit_targets"] = {hkey_none: {
            "kind": "form", "mode": "update", "prefixes": ("/admin-api/system/user/",),
            "record_id": "5", "baseline": {"id": 5, "email": "old@example.com"},
        }}
        ans_none = self._run_loop(loop_none)
        self.assertEqual(loop_none.state.terminal_reason, "agent_refused")
        self.assertIn("没有看到你点击", ans_none)
        self.assertIn("尚未落库", ans_none)

        # 4. Readback failure after accepted write still refuses completion
        self.intent("save", "把员工邮箱改成 ops@example.com")
        loop_rb = Loop([fill(), finish()])
        hkey_rb = _handoff_key(loop_rb.state.obs, "保存")
        loop_rb.native_submit = NS(
            {"method": "PUT", "path": "/admin-api/system/user/update", "status": 200, "record_id": "5", "accepted": True},
            None,
        )
        loop_rb.turn["handoff_target_key"] = hkey_rb
        loop_rb.turn["submit_targets"] = {hkey_rb: {
            "kind": "form", "mode": "update", "prefixes": ("/admin-api/system/user/",),
            "record_id": "5", "baseline": {"id": 5, "email": "old@example.com"},
        }}
        ans_rb = self._run_loop(loop_rb)
        self.assertEqual(loop_rb.state.terminal_reason, "agent_refused")
        self.assertIn("回读记录没有成功", ans_rb)

        # 5. Draft-only intent never waits for click
        self.intent("draft", "名称先填Synthetic，由我提交")
        loop_draft = Loop([fill(), finish()])
        loop_draft.native_submit = NS(None)
        loop_draft.capture_form_baseline = lambda *_: None
        hkey_draft = _handoff_key(loop_draft.state.obs, "保存")
        loop_draft.turn["handoff_target_key"] = hkey_draft
        loop_draft.turn["submit_targets"] = {hkey_draft: {
            "kind": "form", "mode": "update", "prefixes": ("/admin-api/system/user/",), "record_id": "5",
        }}
        loop_draft._await_handoff_press = lambda *_: self.fail("draft handoff must not wait for click")
        ans_draft = self._run_loop(loop_draft)
        self.assertEqual(loop_draft.state.terminal_reason, "")
        self.assertIn("尚未提交", ans_draft)

    def test_handoff_deduplicates_restricted_password_field_alias_and_human_label(self):
        from web_operation_agent.execution.loop_guard import build_handoff_payload
        from web_operation_agent_yudao.executor.procedures import build_handoff_facts

        obs = saving_form()
        pwd = replace(
            obs.elements[0],
            ref="e7",
            name="用户密码",
            attrs={"form_scope": "form:demo", "field_label": "用户密码", "field": "password", "input_type": "password"},
        )
        obs = replace(obs, elements=[*obs.elements, pwd])
        turn = {"field_outcomes": {
            "applied_fields": {"用户昵称": "林晓峰"},
            "rejected_fields": {},
            "previous_fields": {"用户昵称": ""},
            "restricted_fields": ["password", "用户密码"],
        }}
        facts = build_handoff_facts(turn, obs)
        self.assertEqual(facts["restricted_controls"], [{"label": "用户密码", "kind": "password", "ref": "e7"}])
        self.assertEqual(facts["pending_fields"], [{"name": "用户密码"}])

        core_payload = build_handoff_payload(turn["field_outcomes"], observation=obs)
        self.assertEqual(core_payload["restricted_controls"], [{"label": "用户密码", "kind": "password", "ref": "e7"}])

        # Also merge when DOM only exposes generic ASCII "password" while field_outcomes supplies "用户密码"
        ascii_pwd = replace(
            obs.elements[0],
            ref="e12",
            name="password",
            attrs={"form_scope": "form:demo", "input_type": "password"},
        )
        obs2 = replace(saving_form(), elements=[*saving_form().elements, ascii_pwd])
        turn2 = {"field_outcomes": {
            "applied_fields": {"用户昵称": "林晓峰"},
            "rejected_fields": {},
            "previous_fields": {"用户昵称": ""},
            "restricted_fields": ["用户密码"],
        }}
        facts2 = build_handoff_facts(turn2, obs2)
        self.assertEqual(facts2["restricted_controls"], [{"label": "用户密码", "kind": "password", "ref": "e12"}])
        self.assertEqual(facts2["pending_fields"], [{"name": "用户密码"}])

    def test_form_handoff_infers_prefix_without_baseline_and_awaits_create(self):
        class CreatePort:
            def recent_reads(self):
                return (("/admin-api/demo/item/page", "pageNo=1"),)

            def prefixes(self, _reads, *, mode, record_id, source=""):
                return ("/admin-api/demo/item/",) if source in {"/admin-api/demo/item/page", ""} else ()

            def user_write_mark(self):
                return 7

            def user_write_event(self, prefixes, record_id, mark, create=False):
                if not create and prefixes == ("/admin-api/demo/item/",):
                    return {"method": "POST", "path": "/admin-api/demo/item/create",
                            "status": 200, "record_id": "19", "accepted": True}
                return None

            def read_record(self, prefixes, record_id, *, context_params=None):
                return {"id": 19, "name": "Synthetic"}

        self.intent("save", "新建条目，名称 Synthetic")
        loop = Loop([fill(), finish()])
        loop.state.obs = replace(loop.state.obs, page_read={
            "path": "/admin-api/demo/item/page",
            "query_digest": "a" * 64,
            "query_fields": {},
            "revision": 1,
            "page": 1,
            "page_size": 10,
            "total": 1,
            "rows": 1,
        })
        loop.native_submit = CreatePort()
        loop.turn["handoff_target_key"] = "form:k"
        loop.turn["submit_targets"] = {"form:k": {"kind": "form"}}
        ctx = loop._build_post_handoff_watch_context(loop.state, {"handoff": ("fp", "保存"), **loop.turn})
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx["prefixes"], ("/admin-api/demo/item/",))
        self.assertEqual(ctx["mark"], 7)
        self.assertFalse(ctx["create"])

        answer = self._run_loop(loop)
        self.assertEqual(loop.state.terminal_reason, "")
        self.assertIn("编号 19", answer)
        self.assertNotIn("尚未提交", answer)

    def test_edit_form_without_record_id_observes_update_through_page_guard_and_reads_back(self):
        from contextlib import contextmanager
        from web_operation_agent_yudao.host.page_guard import NativePageGuard
        from web_operation_agent_yudao.writes.native_submit import YudaoSubmitPort

        docs = {"paths": {
            "/admin-api/demo/item/update": {"put": {}},
            "/admin-api/demo/item/get": {"get": {}},
            "/admin-api/demo/item/page": {"get": {}},
        }}
        guard = NativePageGuard(frozenset(), writing_gets=frozenset())
        actor = "a" * 64
        guard.note_read(actor, "/admin-api/demo/item/page?pageNo=1")
        client = NS(
            _login=lambda: "u",
            tenant="1",
            get=lambda path, **_kw: {"id": 42, "remark": "已更新内容"} if path == "demo/item/get" else None,
        )
        proxy = NS(guard=guard, resolve_actor=lambda *_a: actor)
        port = YudaoSubmitPort(client, proxy, lambda: docs)

        self.intent("save", "把备注改成已更新内容")
        loop = Loop([fill(label="备注", value="已更新内容"), finish()])
        loop.native_submit = port

        @contextmanager
        def simulate_user_update():
            guard.note_user_write(actor, "PUT", "/admin-api/demo/item/update", record_id="42", status=200, accepted=True)
            yield

        loop.state.approvals = NS(approval_hold_ms=1000, awaiting_person=simulate_user_update, handoff_wait_deadline=None)
        answer = self._run_loop(loop)
        self.assertEqual(loop.state.terminal_reason, "")
        self.assertEqual(loop.turn.get("handoff_wait_outcome"), "verified")
        self.assertIn("回读确认记录（编号 42）", answer)
        self.assertIn("remark：已更新内容", answer)
        self.assertNotIn("尚未落库", answer)

    def test_slim_verify_runs_before_handoff_wait_and_preserves_verified_readback(self):
        from web_operation_agent.execution.model_loop_contracts import VerificationResult
        from web_operation_agent_yudao.executor.slim_loop import _handoff_key

        self.intent("save", "把员工邮箱改成 ops@example.com")
        loop = Loop([fill(), finish()])
        loop._unsaid = lambda *_a, **_kw: ["email"]
        seen_drafts = []

        def fake_verify(_s, _prompt, answer, _turn):
            seen_drafts.append(answer)
            if "应用已接受写入" in answer:
                return VerificationResult("disputed", ("未见到写入工具调用",))
            return VerificationResult("passed", ())

        loop._verify = fake_verify
        hkey = _handoff_key(loop.state.obs, "保存")

        class Port:
            def contract(self):
                from web_operation_agent.execution.submit_contract import SubmitConventions
                return SubmitConventions(
                    volatile_keys=frozenset({"updateTime"}),
                    record_key="id", line_key="id", name_key="name", epoch_zone=None,
                    decode_write=lambda status, body: (status, None),
                )

            def user_write_mark(self): return 1

            def user_write_event(self, prefixes, record_id, mark, create=False):
                return {"method": "PUT", "path": "/admin-api/system/user/update", "status": 200, "record_id": "5", "accepted": True}

            def read_record(self, prefixes, record_id, *, context_params=None):
                return {"id": 5, "email": "ops@example.com"}

        loop.native_submit = Port()
        loop.turn["handoff_target_key"] = hkey
        loop.turn["submit_targets"] = {hkey: {
            "kind": "form", "mode": "update", "prefixes": ("/admin-api/system/user/",),
            "record_id": "5", "baseline": {"id": 5, "email": "old@example.com"},
        }}
        answer = self._run_loop(loop)
        self.assertEqual(len(seen_drafts), 1)
        self.assertNotIn("应用已接受写入", seen_drafts[0])
        self.assertIn("email：old@example.com→ops@example.com", answer)
        self.assertEqual(loop.turn.get("handoff_wait_outcome"), "verified")


if __name__ == "__main__":
    unittest.main()
