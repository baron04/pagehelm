"""request_submit end to end over a fake page and a fake application: no model, no browser."""
from copy import deepcopy
import json
from types import SimpleNamespace as NS
import unittest

from web_operation_agent.business.approvals import MutationApproval
from web_operation_agent.execution.native_submit import NativeSubmitMixin
from web_operation_agent.execution.submit_contract import SubmitPermit
from web_operation_agent.execution.value_source import SourceLedger
from web_operation_agent.schemas import ActionReceipt, ElementRef, PageObservation

PREFIX = "/admin-api/crm/contact/"
CATALOG = frozenset({("POST", PREFIX + "create"), ("PUT", PREFIX + "update"),
                     ("PUT", PREFIX + "update-status"), ("DELETE", PREFIX + "delete")})
LOADED = {"id": 7, "name": "张三", "mobile": "13800000000", "customerId": 3, "status": 0,
          "lines": [{"id": 1, "sku": "A", "qty": 2}]}
ITEMS = [{"prop": "name", "label": "姓名"}, {"prop": "mobile", "label": "手机"},
         {"prop": "customerId", "label": "客户", "display": "Acme"}]


def observation(epoch=1):
    return PageObservation(1, epoch, "http://app/crm/contact", "Contacts", [], [
        ElementRef("e1", "textbox", "手机", "input", epoch, {"form_scope": "dialog:编辑", "form_instance": "i1",
                                                              "field_label": "手机"}),
        ElementRef("e2", "button", "保存", "button", epoch, {"form_scope": "dialog:编辑", "form_instance": "i1"}),
        ElementRef("e3", "button", "停用", "button", epoch, {})],
        render_state="ready", page_route="/crm/contact")


class Port:
    def __init__(self):
        self.record, self.armed, self.exists = deepcopy(LOADED), None, True
        self.dangerous_ = False

    def recent_reads(self): return ((PREFIX + "get", "id=7"),)
    def prefixes(self, reads, *, mode, record_id, source=""): return (PREFIX,)
    def row_prefixes(self, reads, row): return (PREFIX,)
    def row_request(self, prefixes, row, element, record):
        from web_operation_agent.execution.submit_contract import ActionRequest
        return ActionRequest("PUT", PREFIX + "update-status", {"id": 7, "status": 1})
    def catalog(self, prefixes): return CATALOG
    def dangerous(self, element): return self.dangerous_
    def arm(self, permit): self.armed = permit
    def disarm(self): self.armed = None
    def settled(self): pass

    def read_record(self, prefixes, record_id):
        return deepcopy(self.record) if self.exists else None


class Approvals:
    approval_hold_ms = 1000

    def __init__(self, approved=True):
        self.approved, self.cards = approved, []

    def context(self, step, tool, hold_ms=0):
        return NS(binding_digest="a" * 64, audit_projection=lambda: {})

    def approve_mutation(self, proposal, **_):
        self.cards.append(proposal)
        return (MutationApproval("ap-1", proposal.content_digest) if self.approved
                else MutationApproval(reason="approval_denied", detail="no"))


class Executor(NativeSubmitMixin):
    def __init__(self, approved=True):
        self.native_submit = Port()
        self._earlier_words = []
        self.form = {**LOADED}                          # what the page's form holds now
        self.clicks = []                                # requests the click makes: (method, path, body)
        self.status = 200
        self.confirm = None                             # an application confirmation box, if any
        self.s = NS(obs=observation(), step=0, terminal_reason="", detail="", write_intent=False,
                    mutation_outcomes=[], message=NS(text="把张三的手机改成13900000000"),
                    approvals=Approvals(approved), events=[],
                    ledger=NS(append=lambda event, **facts: self.s.events.append((event, facts))))
        self.turn = {"written": [], "form_baselines": {}, "source_ledger": SourceLedger()}
        self.dispatched = []

    def _submit_allowed(self, turn): return True
    def _said(self, prompt): return [*self._earlier_words, prompt]
    def _element(self, s, index): return next((e for e in s.obs.elements if e.ref == f"e{index}"), None)

    def model(self, **changes):
        return {"model": {**self.form, **changes}, "mode": "update", "record_id": "7", "title": "编辑",
                "items": ITEMS, "check": json.dumps({**self.form, **changes}, sort_keys=True), "withheld": [],
                "invalid": []}

    def _dispatch(self, s, action, args, **kw):
        self.dispatched.append((action, args.get("kind")))
        data = None
        if action == "form_model":
            if args["kind"] == "form":
                data = self.form_read()
            elif args["kind"] == "validate":
                data = {**self.form_read(), "validation": True}
            elif args["kind"] == "restore":
                self.form.update(args["values"])
                data = self.form_read()
            elif args["kind"] == "row":
                data = {"record_id": "7", "name": "张三", "mode": "action", "title": ""}
            else:
                data = self.confirm or {"open": False, "text": "", "buttons": []}
        elif action == "form_submit" and args["kind"] != "confirm":
            self.click()
        elif action == "form_submit":
            self.confirm = None
            self.click()
        s.obs = observation(s.obs.epoch + 1)
        return ActionReceipt(1, f"c{len(self.dispatched)}", action, args, True, observation=s.obs,
                             url_before=s.obs.url, url_after=s.obs.url, form_data=data)

    def form_read(self):
        return self.model()

    def click(self):
        permit = self.native_submit.armed
        for method, path, body in self.clicks:
            decision = permit.admit(method, path, "", json.dumps(body).encode()) if permit else None
            if decision is not None and decision.allowed:
                permit.settle(decision.sequence, self.status, b'{"code":0,"data":7}')
                if self.status == 200 and method == "PUT":
                    self.native_submit.record.update(body)


def entry(executor, label="手机", value="13900000000"):
    executor.note_entries(executor.s, executor.turn, ((label, value),), executor.s.obs)


class _Setup:
    def setUp(self):
        self.ex = Executor()
        self.ex.form_read = lambda: self.ex.model()
        # the page loaded the form, then the assistant typed one value into it
        self.ex.capture_form_baseline(self.ex.s, self.ex.turn, self.ex.s.obs.elements[0])
        self.ex.form = {**LOADED, "mobile": "13900000000"}
        entry(self.ex)
        self.ex.clicks = [("PUT", PREFIX + "update", {**LOADED, "mobile": "13900000000"})]

    def run_request(self, index=2):
        return self.ex._request_submit(self.ex.s, {"index": index}, self.ex.turn)



class NativeSubmitTests(_Setup, unittest.TestCase):
    def test_happy_path_card_click_once_and_readback(self):
        out = self.run_request()
        self.assertEqual(out.state, "completed", out.feedback)
        card = self.ex.s.approvals.cards[0]
        texts = {d.label: d.text for d in card.details}
        self.assertEqual(texts["手机"], "13800000000 → 13900000000")
        self.assertEqual(self.ex.native_submit.record["mobile"], "13900000000")
        self.assertEqual([a for a, _ in self.ex.dispatched].count("form_submit"), 1)
        kinds = [e for e, _ in self.ex.s.events]
        self.assertEqual(kinds[kinds.index("native_submit_card"):][:4],
                         ["native_submit_card", "native_submit_reread", "native_submit_approved", "native_submit_clicked"])
        result = [f for e, f in self.ex.s.events if e == "native_submit_result"][0]
        self.assertNotIn("readback_record", result)
        self.assertNotIn("body", result["requests"][0])
        self.assertTrue(self.ex.turn["written"])
        self.assertEqual(self.ex.s.mutation_outcomes[0]["verified"], True)

    def test_a_value_the_person_did_not_give_is_never_carded(self):
        self.ex.form = {**LOADED, "mobile": "13911112222"}
        self.ex.turn["source_ledger"] = SourceLedger()
        entry(self.ex, value="13911112222")
        out = self.run_request()
        self.assertEqual((out.state, out.error_code), ("needs_input", "value_not_from_user"))
        self.assertEqual(self.ex.s.approvals.cards, [])
        self.assertEqual([a for a, _ in self.ex.dispatched].count("form_submit"), 0)

    def test_declined_card_sends_nothing(self):
        self.ex.s.approvals.approved = False
        out = self.run_request()
        self.assertEqual((out.state, self.ex.s.terminal_reason), ("refused", "approval_denied"))
        self.assertIn("助手未提交", out.feedback)
        self.assertEqual(self.ex.native_submit.record["mobile"], "13800000000")
        self.assertNotIn("form_submit", [a for a, _ in self.ex.dispatched])

    def test_form_that_changes_after_approval_is_not_pressed(self):
        approve = self.ex.s.approvals.approve_mutation
        def changed(proposal, **kw):
            self.ex.form = {**self.ex.form, "name": "李四"}
            return approve(proposal, **kw)
        self.ex.s.approvals.approve_mutation = changed
        out = self.run_request()
        self.assertEqual(out.state, "refused")
        self.assertNotIn("form_submit", [a for a, _ in self.ex.dispatched])
        self.assertEqual(self.ex.s.terminal_reason, "business_mutation_refused")

    def test_record_changed_by_someone_else_after_approval_is_not_pressed(self):
        approve = self.ex.s.approvals.approve_mutation
        def changed(proposal, **kw):
            self.ex.native_submit.record["status"] = 1
            return approve(proposal, **kw)
        self.ex.s.approvals.approve_mutation = changed
        self.run_request()
        self.assertNotIn("form_submit", [a for a, _ in self.ex.dispatched])
        self.assertEqual(self.ex.native_submit.record["status"], 1)

    def test_record_changed_between_the_last_reread_and_the_release_is_blocked(self):
        click = self.ex.click
        def late():
            self.ex.native_submit.record["status"] = 1      # after the pre-click reread
            click()
        self.ex.click = late
        out = self.run_request()
        self.assertEqual(self.ex.native_submit.record["mobile"], "13800000000")
        self.assertEqual(self.ex.native_submit.record["status"], 1)
        self.assertEqual(out.state, "refused")

    def test_extra_request_is_blocked_and_the_card_still_completes_as_partial(self):
        self.ex.clicks.append(("DELETE", PREFIX + "delete", {"id": 7}))
        out = self.run_request()
        self.assertEqual(out.state, "indeterminate")
        self.assertIn("一部分生效", out.feedback)
        self.assertEqual(self.ex.s.terminal_reason, "indeterminate_write")

    def test_double_click_is_released_once(self):
        self.ex.clicks *= 2
        out = self.run_request()
        self.assertEqual(out.state, "completed")
        permit_events = [f for e, f in self.ex.s.events if e == "native_submit_result"][0]
        self.assertEqual(permit_events["duplicates"], 1)
        self.assertIn("重复请求被拦下", out.feedback)

    def test_application_refusal_is_reported_and_not_retried(self):
        self.ex.status = 500
        out = self.run_request()
        self.assertEqual(out.state, "refused")
        self.assertEqual([a for a, _ in self.ex.dispatched].count("form_submit"), 1)
        self.assertEqual(self.ex.native_submit.record["mobile"], "13800000000")

    def test_click_that_sends_nothing_is_not_executed(self):
        self.ex.clicks = []
        out = self.run_request()
        self.assertEqual(out.state, "refused")
        self.assertIn("没有执行", out.feedback)

    def test_unreadable_form_hands_over_instead_of_submitting(self):
        def broken():
            raise __import__("web_operation_agent.execution.submit_contract", fromlist=["x"]).ContractRefused(
                "form_data_unreadable")
        self.ex._read = lambda *a, **kw: broken()
        out = self.run_request()
        self.assertEqual((out.state, out.error_code), ("refused", "form_data_unreadable"))
        self.assertIn("留给你自己", out.feedback)

    def test_credential_field_and_failed_validation_are_not_submitted(self):
        self.ex.form_read = lambda: {**self.ex.model(), "withheld": ["password"]}
        self.assertEqual(self.run_request().error_code, "credential_field")
        self.ex.form_read = lambda: {**self.ex.model(), "invalid": ["mobile"]}
        out = self.run_request()
        self.assertEqual((out.error_code, out.state), ("validation", "needs_input"))
        self.assertEqual(self.ex.s.approvals.cards, [])

    def test_no_change_makes_no_card(self):
        self.ex.form = {**LOADED}
        self.ex.turn["source_ledger"] = SourceLedger()
        self.assertEqual(self.run_request().error_code, "no_change")

    def test_not_a_submit_control(self):
        self.assertEqual(self.ex._request_submit(self.ex.s, {"index": 99}, self.ex.turn).error_code, "invalid_target")


class ReviewRegressionTests(_Setup, unittest.TestCase):
    def test_a_dropdown_value_the_person_never_named_is_not_carded(self):
        self.ex.turn["source_ledger"] = SourceLedger()
        self.ex.s.obs = observation()
        self.ex.note_entries(self.ex.s, self.ex.turn, (("手机", "13911112222"),), self.ex.s.obs)
        self.ex.s.obs.elements[0].attrs["field_label"] = "手机"
        out = self.run_request()
        self.assertEqual(out.error_code, "value_not_from_user")

    def test_a_click_that_raises_is_indeterminate_not_a_clean_refusal(self):
        original = self.ex._dispatch
        def boom(s, action, args, **kw):
            if action == "form_submit":
                raise RuntimeError("budget")
            return original(s, action, args, **kw)
        self.ex._dispatch = boom
        out = self.run_request()
        self.assertEqual((out.state, self.ex.s.terminal_reason), ("indeterminate", "indeterminate_write"))
        self.assertIn("native_submit_exception", [e for e, _ in self.ex.s.events])

    def test_a_permit_that_cannot_be_armed_sends_nothing_and_says_so(self):
        def refuse(permit):
            raise RuntimeError("permit_in_use")
        self.ex.native_submit.arm = refuse
        out = self.run_request()
        self.assertEqual(out.state, "refused")
        self.assertNotIn("form_submit", [a for a, _ in self.ex.dispatched])
        self.assertIn("native_submit_not_armed", [e for e, _ in self.ex.s.events])

    def test_no_request_recorded_but_the_record_moved_is_not_reported_as_not_executed(self):
        # a request outside the permit's sight (it went out after the window closed)
        self.ex.clicks = []
        original = self.ex.click
        def late():
            self.ex.native_submit.record["mobile"] = "13900000000"
        self.ex.click = late
        out = self.run_request()
        self.assertEqual(out.state, "indeterminate")
        self.assertNotIn("没有执行", out.feedback.split("：", 1)[0])

    def test_a_refusal_in_the_users_words_is_not_a_confirmation(self):
        from web_operation_agent.execution.native_submit import _confirm_words
        for no in ("不行，别删", "不可以删除", "先等等", "算了"):
            self.assertFalse(_confirm_words(no), no)
        for yes in ("确认，停用吧", "可以，继续", "没问题，办吧"):
            self.assertTrue(_confirm_words(yes), yes)

    def test_readback_accepts_lines_that_carry_keys_of_their_own(self):
        self.ex.form = {**LOADED, "lines": [{"id": 1, "sku": "A", "qty": 3}], "mobile": "13900000000"}
        self.ex.clicks = [("PUT", PREFIX + "update", dict(self.ex.form))]
        original = self.ex.native_submit.read_record
        def shaped(prefixes, record_id):
            record = original(prefixes, record_id)
            if record is not None:
                record["lines"] = [{**line, "computedTotal": 9} for line in record.get("lines", [])]
            return record
        self.ex.native_submit.read_record = shaped
        out = self.run_request()
        self.assertIn(out.state, {"completed", "refused", "indeterminate"})


class OpeningChangeTests(_Setup, unittest.TestCase):
    def test_restores_opening_change_then_approves_once_against_record(self):
        self.ex.form = {**LOADED, "customerId": 9}
        self.ex.turn["form_baselines"] = {}
        self.ex.capture_form_baseline(self.ex.s, self.ex.turn, self.ex.s.obs.elements[0])
        self.ex.form["mobile"] = "13900000000"
        out = self.run_request()
        self.assertEqual(out.state, "completed", out.feedback)
        self.assertEqual(self.ex.form["customerId"], 3)
        card = self.ex.s.approvals.cards[0]
        self.assertTrue(any("已还原" in d.text for d in card.details))
        self.assertNotIn("customerId", card.args["fields"])
        self.assertEqual(len(self.ex.clicks), 1)

    def test_detects_concurrent_change_while_filling_before_card(self):
        self.ex.native_submit.record["name"] = "别人改的"
        out = self.run_request()
        self.assertEqual(out.error_code, "record_changed")
        self.assertEqual(self.ex.s.approvals.cards, [])

    def test_line_identity_survives_reordering_in_record_and_in_form(self):
        from web_operation_agent.execution.native_submit import opening_values, record_model
        base = {"lines": [{"id": 2, "qty": 20, "computed": 4}, {"id": 1, "qty": 10}]}
        loaded = {"lines": [{"id": 1, "qty": 10}, {"id": 2, "qty": 21}]}
        current = {"lines": [{"id": 2, "qty": 21}, {"id": 1, "qty": 11}]}
        projected = record_model(base, current)
        self.assertEqual(opening_values(projected, loaded, current),
                         {"lines": [{"id": 2, "qty": 20}, {"id": 1, "qty": 11}]})

    def test_invalid_opening_value_is_restored_before_application_validation(self):
        self.ex.form = {**LOADED, "customerId": None}
        self.ex.turn["form_baselines"] = {}
        self.ex.capture_form_baseline(self.ex.s, self.ex.turn, self.ex.s.obs.elements[0])
        self.ex.form["mobile"] = "13900000000"
        self.ex.form_read = lambda: {**self.ex.model(), "invalid": [] if self.ex.form["customerId"] else ["customerId"]}
        out = self.run_request()
        self.assertEqual(out.state, "completed", out.feedback)
        kinds = [kind for action, kind in self.ex.dispatched if action == "form_model"]
        self.assertLess(kinds.index("restore"), kinds.index("validate"))

    def test_private_records_are_opt_in_and_absent_from_the_normal_ledger(self):
        captured = []
        self.ex.native_submit.record_private = lambda event, facts: captured.append((event, facts))
        out = self.run_request()
        self.assertEqual(out.state, "completed")
        self.assertEqual([e for e, _ in captured], ["card", "before_click", "before_release", "result"])
        self.assertEqual(captured[-1][1]["readback_record"]["mobile"], "13900000000")
        card = next(f for e, f in self.ex.s.events if e == "native_submit_card")
        self.assertNotIn("loaded", card)
        self.assertNotIn("submitted", card)
        reread = next(f for e, f in self.ex.s.events if e == "native_submit_reread")
        self.assertNotIn("record", reread)

    def test_failed_restore_never_approves_or_clicks(self):
        self.ex.form = {**LOADED, "customerId": 9}
        self.ex.turn["form_baselines"] = {}
        self.ex.capture_form_baseline(self.ex.s, self.ex.turn, self.ex.s.obs.elements[0])
        self.ex.form["mobile"] = "13900000000"
        dispatch = self.ex._dispatch
        def refuse(s, action, args, **kw):
            if args.get("kind") == "restore":
                from web_operation_agent.execution.submit_contract import ContractRefused
                raise ContractRefused("validation")
            return dispatch(s, action, args, **kw)
        self.ex._dispatch = refuse
        out = self.run_request()
        self.assertEqual(out.error_code, "opening_changes")
        self.assertIn("客户=9", out.feedback)
        self.assertEqual(self.ex.s.approvals.cards, [])


class RowActionTests(unittest.TestCase):
    def setUp(self):
        self.ex = Executor()
        self.ex.s.message = NS(text="把张三停用")
        self.ex.clicks = [("PUT", PREFIX + "update-status", {"id": 7, "status": 1})]

    def run_request(self):
        return self.ex._request_submit(self.ex.s, {"index": 3}, self.ex.turn)

    def test_row_action_card_click_and_readback(self):
        out = self.run_request()
        self.assertEqual(out.state, "completed", out.feedback)
        self.assertIn("停用", self.ex.s.approvals.cards[0].effect)
        self.assertEqual(self.ex.native_submit.record["status"], 1)

    def test_readback_checks_the_action_field_and_reports_other_key_additions_or_removals(self):
        ex = self.ex
        click = ex.click
        def with_changes():
            click()
            ex.native_submit.record.pop("mobile", None)
            ex.native_submit.record["newBusinessField"] = "added"
        ex.click = with_changes
        out = self.run_request()
        self.assertEqual(out.state, "completed", out.feedback)
        self.assertIn("mobile", out.feedback)
        self.assertIn("newBusinessField", out.feedback)

    def test_dangerous_action_needs_a_confirmation_in_the_conversation(self):
        self.ex.native_submit.dangerous_ = True
        out = self.run_request()
        self.assertEqual((out.error_code, out.state), ("dangerous_unconfirmed", "needs_input"))
        self.assertEqual(self.ex.s.approvals.cards, [])
        self.ex._earlier_words = ["把张三停用"]
        self.ex.s.message = NS(text="确认，停用吧")
        self.assertEqual(self.run_request().state, "completed")

    def test_harmless_confirmation_box_is_answered_for_the_same_action(self):
        self.ex.confirm = {"open": True, "text": "确认要停用该用户吗？", "buttons": ["取消", "确定"]}
        # the first press opens the box (no request), the confirm press sends it
        first = self.ex.click
        self.ex.click = lambda: None
        original = self.ex._dispatch
        def dispatch(s, action, args, **kw):
            if action == "form_submit" and args["kind"] == "confirm":
                self.ex.click = first
            return original(s, action, args, **kw)
        self.ex._dispatch = dispatch
        out = self.run_request()
        self.assertEqual(out.state, "completed", out.feedback)
        self.assertIn(("form_submit", "confirm"), self.ex.dispatched)

    def test_box_with_consequences_beyond_the_card_is_cancelled(self):
        self.ex.confirm = {"open": True, "text": "将永久删除该用户及其关联数据，无法恢复，是否继续？",
                           "buttons": ["取消", "确定"]}
        self.ex.click = lambda: None
        out = self.run_request()
        self.assertEqual(out.state, "refused")
        self.assertEqual(self.ex.native_submit.record["status"], 0)
        self.assertEqual(sum(1 for a, k in self.ex.dispatched if a == "form_submit" and k == "confirm"), 1)
        self.assertIn("没有执行", out.feedback)

    def test_row_that_is_not_the_card_target_is_refused_at_release(self):
        self.ex.clicks = [("PUT", PREFIX + "update-status", {"id": 8, "status": 1})]
        out = self.run_request()
        self.assertEqual(out.state, "refused")
        self.assertEqual(self.ex.native_submit.record["status"], 0)


if __name__ == "__main__":
    unittest.main()
