"""Native submission over ChannelServer and synthetic page/application: no paid model."""
from copy import deepcopy
from datetime import timedelta, timezone
import json
from types import SimpleNamespace as NS
import unittest

from web_operation_agent.business.approvals import MutationApproval
from web_operation_agent.execution.native_submit import NativeSubmitMixin
from web_operation_agent.execution.submit_contract import SubmitConventions, SubmitPermit
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


def _decode(status, body):
    answer = None
    if body and status is not None and 200 <= status < 300:
        try:
            envelope = json.loads(body.decode("utf-8"))
            answer = envelope.get("data") if isinstance(envelope, dict) else None
            if isinstance(envelope, dict) and envelope.get("code") not in (0, None):
                status = 500
            if isinstance(answer, (dict, list)):
                answer = None
        except (UnicodeDecodeError, ValueError):
            pass
    return status, answer


CONVENTIONS = SubmitConventions(
    volatile_keys=frozenset({"updateTime", "updater", "updaterName", "modifyTime", "version"}),
    record_key="id", line_key="id", name_key="name", epoch_zone=timezone(timedelta(hours=8)),
    decode_write=_decode)


class Port:
    def __init__(self):
        self.record, self.armed, self.exists = deepcopy(LOADED), None, True
        self.dangerous_ = False

    def contract(self): return CONVENTIONS

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
        from web_operation_agent.tests.transport.submit_wire import SubmitWire
        self.wire = SubmitWire(self.s.obs)

    def _submit_allowed(self, turn): return True
    def _invoke_tool(self, s, name, args, turn):
        from web_operation_agent.execution.model_loop_contracts import LoopToolResult
        return LoopToolResult("目标交接未核验", kind="page", state="refused")
    def _said(self, prompt): return [*self._earlier_words, prompt]
    def _element(self, s, index): return next((e for e in s.obs.elements if e.ref == f"e{index}"), None)

    def model(self, **changes):
        return {"model": {**self.form, **changes}, "mode": "update", "record_id": "7", "title": "编辑",
                "items": ITEMS, "check": json.dumps({**self.form, **changes}, sort_keys=True), "withheld": [],
                "invalid": []}

    def _dispatch(self, s, action, args, **kw):
        issued = self.wire.command(action, args)
        receipt = self._browser(s, action, issued.command.args, **kw)
        return self.wire.result(issued, receipt)

    def _browser(self, s, action, args, **kw):
        self.dispatched.append((action, args.get("kind")))
        data = None
        if action == "form_model":
            if args["kind"] == "form":
                data = self.form_read()
            elif args["kind"] == "validate":
                data = {**self.form_read(), "validation": True}
            elif args["kind"] == "restore":
                self.form.update(args["values"])
                data = {**self.form_read(), "validation": True}
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

    def test_a_value_without_a_source_is_refused_before_the_card(self):
        self.ex.form = {**LOADED, "mobile": "13911112222"}
        self.ex.turn["source_ledger"] = SourceLedger()
        entry(self.ex, value="13911112222")
        out = self.run_request()
        self.assertEqual((out.state, out.error_code), ("needs_input", "value_not_from_user"))
        self.assertIn("手机=13911112222", out.feedback)
        self.assertEqual(self.ex.s.approvals.cards, [])
        self.assertNotIn("form_submit", [a for a, _ in self.ex.dispatched])
        self.assertEqual(self.ex.turn["submit_handoff_blocked"], "value_not_from_user")

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
        self.assertEqual((out.state, out.error_code), ("unverified", "form_data_unreadable"))
        self.assertIn("留给你自己", out.feedback)

    def test_credential_field_and_failed_validation_are_not_submitted(self):
        self.ex.form_read = lambda: {**self.ex.model(), "withheld": ["password"]}
        self.assertEqual(self.run_request().error_code, "credential_field")
        self.ex.form_read = lambda: {**self.ex.model(), "invalid": ["mobile"]}
        out = self.run_request()
        self.assertEqual((out.error_code, out.state), ("validation", "needs_input"))
        self.assertEqual(self.ex.s.approvals.cards, [])

    def test_a_port_without_declared_conventions_cannot_submit(self):
        self.ex.native_submit = NS(contract=lambda: None)
        out = self.run_request()
        self.assertEqual((out.state, out.error_code), ("unverified", "conventions_undeclared"))
        self.assertNotIn("form_submit", [a for a, _ in self.ex.dispatched])
        del self.ex.native_submit.contract
        out = self.run_request()
        self.assertEqual((out.state, out.error_code), ("unverified", "conventions_undeclared"))

    def test_no_change_makes_no_card(self):
        self.ex.form = {**LOADED}
        self.ex.turn["source_ledger"] = SourceLedger()
        self.assertEqual(self.run_request().error_code, "no_change")

    def test_not_a_submit_control(self):
        self.assertEqual(self.ex._request_submit(self.ex.s, {"index": 99}, self.ex.turn).error_code, "invalid_target")


class ReviewRegressionTests(_Setup, unittest.TestCase):
    def test_a_non_candidate_value_is_refused_even_when_the_control_looks_like_a_dropdown(self):
        self.ex.turn["source_ledger"] = SourceLedger()
        self.ex.note_entries(self.ex.s, self.ex.turn, (("手机", "13911112222"),), self.ex.s.obs)
        self.ex.s.obs.elements[0].attrs["field_label"] = "手机"
        out = self.run_request()
        self.assertEqual((out.state, out.error_code), ("needs_input", "value_not_from_user"))
        self.assertEqual(self.ex.s.approvals.cards, [])

    def test_a_value_from_a_record_read_this_turn_counts_as_a_candidate(self):
        self.ex.form = {**LOADED, "mobile": "13911112222"}
        self.ex.turn["source_ledger"] = SourceLedger()
        self.ex.turn["source_ledger"].add_candidates(
            [("手机", "13911112222"), ("手机", "别的记录")])
        entry(self.ex, value="13911112222")
        self.ex.clicks = [("PUT", PREFIX + "update", {**LOADED, "mobile": "13911112222"})]
        out = self.run_request()
        self.assertEqual(out.state, "completed", out.feedback)
        checks = [f for e, f in self.ex.s.events if e == "native_submit_value_sources"][0]["checks"]
        self.assertEqual(checks[0]["source"], "candidate")

    def test_a_receipt_for_another_field_does_not_source_a_value(self):
        self.ex.form = {**LOADED, "mobile": "13911112222"}
        self.ex.turn["source_ledger"] = SourceLedger()
        entry(self.ex, value="13911112222")
        self.ex.note_receipt(self.ex.turn, "别的字段", "13911112222")
        self.assertEqual(self.run_request().error_code, "value_not_from_user")
        # and an unnamed receipt -- a conversion result not bound to a field -- does
        self.ex.turn["source_ledger"] = SourceLedger()
        entry(self.ex, value="13911112222")
        self.ex.note_receipt(self.ex.turn, "", "13911112222")
        self.ex.clicks = [("PUT", PREFIX + "update", {**LOADED, "mobile": "13911112222"})]
        self.assertEqual(self.run_request().state, "completed")

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

    def test_append_request_refuses_destructive_overwrite_and_accepts_preserved_prefix(self):
        from web_operation_agent.execution.value_source import Entry
        self.ex.native_submit.record["remark"] = "主结算户"
        self.ex.form = {**LOADED, "remark": "主结算户"}
        self.ex.form_read = lambda: {**self.ex.model(), "items": [*ITEMS, {"prop": "remark", "label": "备注"}]}
        self.ex.s.message = NS(text="在张三的备注里追加：限额五万")
        self.ex.turn["form_baselines"] = {}
        self.ex.capture_form_baseline(self.ex.s, self.ex.turn, self.ex.s.obs.elements[0])

        # 1. Overwriting existing remark ("主结算户" -> "限额五万") is refused before the card
        self.ex.form["remark"] = "限额五万"
        self.ex.turn["source_ledger"] = SourceLedger()
        self.ex.turn["source_ledger"].add(Entry("备注", "限额五万"))
        refused = self.ex._request_submit(self.ex.s, {"index": 2}, self.ex.turn)
        self.assertEqual((refused.state, refused.error_code), ("needs_input", "append_overwrite"))
        self.assertEqual(self.ex.s.approvals.cards, [])

        # 2. Preserving existing remark + user addition ("主结算户；限额五万") passes value_source and approves
        self.ex.form["remark"] = "主结算户；限额五万"
        self.ex.turn["source_ledger"] = SourceLedger()
        self.ex.turn["source_ledger"].add(Entry("备注", "主结算户；限额五万"))
        self.ex.clicks = [("PUT", PREFIX + "update", {**self.ex.native_submit.record, "remark": "主结算户；限额五万"})]
        approved = self.ex._request_submit(self.ex.s, {"index": 2}, self.ex.turn)
        self.assertEqual(approved.state, "completed", approved.feedback)
        self.assertEqual(len(self.ex.s.approvals.cards), 1)

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
        projected = record_model(base, current, CONVENTIONS)
        self.assertEqual(opening_values(projected, loaded, current, CONVENTIONS),
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
        self.assertIn("restore", kinds)
        self.assertNotIn("validate", kinds)  # restore itself validates transactionally in the browser

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
        blocked = next(iter(self.ex.turn["unrestorable_forms"].values()))
        self.assertEqual(blocked["scope"], "dialog:编辑")
        self.assertIn("客户：3→9", blocked["effects"])


class ConversionAndHandoffTests(_Setup, unittest.TestCase):
    def opening(self, record, loaded):
        self.ex.native_submit.record = {**LOADED, **record}
        self.ex.form = {**LOADED, **loaded}
        self.ex.turn["form_baselines"] = {}
        self.ex.capture_form_baseline(self.ex.s, self.ex.turn, self.ex.s.obs.elements[0])
        self.ex.form["mobile"] = "13900000000"

    def test_unit_conversion_never_receives_stored_units_and_hands_over(self):
        self.opening({"fee": 250, "ceiling": 1800}, {"fee": "2.50", "ceiling": "18.00"})
        self.ex._default_handoff = lambda *_: "保存"
        out = self.run_request()
        self.assertEqual((out.error_code, out.marked), ("pre_submit_conversion", "保存"))
        self.assertEqual(self.ex.form["fee"], "2.50")
        self.assertEqual(self.ex.s.approvals.cards, [])
        self.assertNotIn("restore", [kind for _, kind in self.ex.dispatched])

    def test_null_placeholders_are_conversions_but_nonzero_defaults_are_restored(self):
        for loaded in (0, "0.00", "1970-01-01"):
            with self.subTest(loaded=loaded):
                self.setUp()
                self.opening({"fee": None}, {"fee": loaded})
                self.assertEqual(self.run_request().error_code, "pre_submit_conversion")
                self.assertEqual(self.ex.form["fee"], loaded)
        for stored, opened in ((None, 13), (None, "auto"), (4, 8)):
            with self.subTest(stored=stored, opened=opened):
                self.setUp()
                self.opening({"other": stored}, {"other": opened})
                self.run_request()
                self.assertEqual(self.ex.form["other"], stored)
                self.assertTrue(self.ex.s.approvals.cards)

    def test_empty_string_placeholders_and_reordered_line_conversions_keep_stored_values_out(self):
        from web_operation_agent.execution.native_submit import conversion_fields, opening_values
        for value in (0, "0.000", "1970-01-01T00:00:00.000Z"):
            self.assertTrue(conversion_fields({"fee": ""}, {"fee": value}, conventions=CONVENTIONS))
            self.assertEqual(opening_values({"fee": ""}, {"fee": value}, {"fee": value}, CONVENTIONS),
                             {"fee": value})
        self.assertFalse(conversion_fields({"fee": ""}, {"fee": ""}, conventions=CONVENTIONS))
        for value in (17, "auto", False):
            self.assertFalse(conversion_fields({"fee": ""}, {"fee": value}, conventions=CONVENTIONS))
            self.assertEqual(opening_values({"fee": ""}, {"fee": value}, {"fee": value}, CONVENTIONS),
                             {"fee": ""})
        record = {"lines": [{"id": 1, "fee": 150}, {"id": 2, "fee": 500}]}
        loaded = {"lines": [{"id": 2, "fee": 500}, {"id": 1, "fee": "1.50"}]}
        self.assertEqual(conversion_fields(record, loaded, conventions=CONVENTIONS),
                         (("lines", "#1", "fee"),))
        self.assertEqual(opening_values(record, loaded, loaded, CONVENTIONS), loaded)

    def test_line_addition_and_deletion_still_restore_old_line_opening_changes_over_the_channel(self):
        old = {"id": 1, "fee": 100, "accountId": 3, "qty": 1}
        opened = {**old, "fee": "1.00", "accountId": 9}
        removed = {"id": 3, "fee": 200, "accountId": 4, "qty": 2}
        for addition in ({"id": 2, "fee": "7.00", "accountId": 12}, {"fee": "7.00", "accountId": 12}):
            for delete in (False, True):
                with self.subTest(addition=addition, delete=delete):
                    self.setUp()
                    self.opening({"lines": [old, removed]}, {"lines": [opened, removed]})
                    self.ex.form["lines"] = [addition, {**opened, "qty": 5}] + ([] if delete else [removed])
                    if delete and "id" not in addition:
                        # Lost old identities plus an unkeyed new row cannot be paired safely.
                        self.ex._default_handoff = lambda *_: self.fail("ambiguous rows were handed over")
                        self.assertEqual(self.run_request().error_code, "opening_changes")
                        continue
                    self.ex._default_handoff = lambda *_: "保存"
                    result = self.run_request()
                    self.assertEqual((result.error_code, result.marked), ("pre_submit_conversion", "保存"))
                    expected = [addition, {**opened, "accountId": 3, "qty": 5}] + ([] if delete else [removed])
                    self.assertEqual(self.ex.form["lines"], expected)
                    restore = [c.args for c in self.ex.wire.commands if c.args.get("kind") == "restore"]
                    self.assertEqual(restore[0]["values"]["lines"], expected)
                    self.assertEqual(self.ex.s.approvals.cards, [])
                    self.assertNotIn("form_submit", [action for action, _ in self.ex.dispatched])

    def test_unmatchable_opening_lines_hard_stop_instead_of_handoff(self):
        cases = (
            ([{"id": 1, "accountId": 3}], [{"id": 1, "accountId": 9}],
             [{"id": 1, "accountId": 9}, {"id": 1, "accountId": 8}]),
            ([{"accountId": 3}], [{"accountId": 9}], [{"accountId": 9}, {"accountId": 8}]),
            ([{"id": 1, "accountId": 3}], [], []),
        )
        for record, loaded, current in cases:
            with self.subTest(record=record, loaded=loaded, current=current):
                self.setUp()
                self.opening({"lines": record}, {"lines": loaded})
                self.ex.form["lines"] = current
                self.ex._default_handoff = lambda *_: self.fail("unmatched page changes were handed over")
                result = self.run_request()
                self.assertEqual(result.error_code, "opening_changes")
                self.assertEqual(self.ex.turn["submit_handoff_blocked"], "opening_changes")
                self.assertFalse(result.marked)
                self.assertEqual(self.ex.s.approvals.cards, [])

    def test_a_conversion_does_not_skip_failed_restoration_or_allow_default_handoff(self):
        self.opening({"fee": 250, "customerId": 3}, {"fee": "2.50", "customerId": 9})
        browser = self.ex._browser
        def refuse(s, action, args, **kw):
            if args.get("kind") == "restore":
                from dataclasses import replace
                s.obs = observation(s.obs.epoch + 1)
                return ActionReceipt(1, "ignored", action, args, False, observation=s.obs,
                                     url_before=s.obs.url, url_after=s.obs.url, error_code="bad_arguments")
            return browser(s, action, args, **kw)
        self.ex._browser = refuse
        self.ex._default_handoff = lambda *_: self.fail("failed restoration must never mark save")
        out = self.run_request()
        self.assertEqual(out.error_code, "opening_changes")
        self.assertEqual(self.ex.turn["submit_handoff_blocked"], "opening_changes")
        self.assertEqual(self.ex.form["fee"], "2.50")
        command = next(c for c in self.ex.wire.commands if c.args.get("kind") == "restore")
        self.assertEqual(command.args["values"], {"customerId": 3})

    def test_query_inputs_are_not_value_sources_and_relative_or_computed_values_do_not_block(self):
        from dataclasses import replace
        query = replace(self.ex.s.obs.elements[0], attrs={"form_scope": "query", "page_control": "query",
                                                       "field_label": "单据号"})
        self.ex.note_entries(self.ex.s, self.ex.turn, (("单据号", "SYN-17"),), replace(self.ex.s.obs, elements=[query]))
        self.assertNotIn("单据号", [e.field for e in self.ex.turn["source_ledger"].entries])
        for words, value, source in (("今天到账", "2026-11-15", "receipt"),
                                     ("加一千五", "9500", "receipt"),
                                     ("用刚才读到的值", "合成封面", "candidate")):
            with self.subTest(words=words):
                self.setUp()
                self.ex.s.message.text = words
                self.ex.form["mobile"] = value
                self.ex.turn["source_ledger"] = SourceLedger()
                if source == "receipt":
                    self.ex.note_receipt(self.ex.turn, "", value)      # date_range / arithmetic result
                else:
                    self.ex.turn["source_ledger"].add_candidates(
                        [("手机", value)])  # a cell in the 手机 column read this turn
                entry(self.ex, value=value)
                self.ex.clicks = [("PUT", PREFIX + "update", self.ex.form)]
                self.assertEqual(self.run_request().state, "completed")

    def test_nested_restore_failure_is_literal_text_and_a_hard_stop(self):
        self.opening({"lines": [{"id": 1, "qty": 2}]},
                     {"lines": [{"id": 1, "qty": 2, "computedTotal": 17}]})
        browser = self.ex._browser
        def refuse(s, action, args, **kw):
            if args.get("kind") == "restore":
                s.obs = observation(s.obs.epoch + 1)
                return ActionReceipt(1, "ignored", action, args, False, observation=s.obs,
                                     url_before=s.obs.url, url_after=s.obs.url, error_code="bad_arguments")
            return browser(s, action, args, **kw)
        self.ex._browser = refuse
        self.ex._default_handoff = lambda *_: self.fail("failed nested restoration must not mark Save")
        result = self.run_request()
        self.assertEqual((result.error_code, result.state), ("opening_changes", "refused"))
        self.assertIn("'computedTotal': 17", result.feedback)
        self.assertIn("{", result.feedback)
        self.assertEqual(self.ex.turn["submit_handoff_blocked"], "opening_changes")
        self.assertEqual(self.ex.s.approvals.cards, [])
        self.assertEqual([c.args.get("kind") for c in self.ex.wire.commands], ["form", "form", "form", "restore"])
        self.assertNotIn("form_submit", [action for action, _ in self.ex.dispatched])

    def test_handoffable_refusals_share_one_path_and_hard_refusals_do_not(self):
        from web_operation_agent.execution.submit_contract import ContractRefused
        for code in ("form_data_unreadable", "entity_unknown", "card_too_large", "pre_submit_conversion"):
            with self.subTest(code=code):
                self.setUp()
                self.ex._submit_form = lambda *_: (_ for _ in ()).throw(ContractRefused(code))
                self.ex._default_handoff = lambda *_: "保存"
                out = self.run_request()
                self.assertEqual(out.marked, "保存")
                self.assertIn(code, str(self.ex.s.events))
        for code in ("dangerous_unconfirmed", "record_changed", "validation", "no_change", "opening_changes"):
            with self.subTest(code=code):
                self.setUp()
                self.ex._submit_form = lambda *_: (_ for _ in ()).throw(ContractRefused(code))
                self.ex._default_handoff = lambda *_: self.fail("hard refusal handed over")
                self.assertFalse(self.run_request().marked)


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

    def test_unknown_action_uses_verified_handoff_without_a_write_permit(self):
        from web_operation_agent.execution.model_loop_contracts import LoopToolResult
        self.ex.native_submit.row_request = lambda *a: None
        handed = []
        def highlight(s, name, args, turn):
            handed.append((name, args))
            turn["marked"] = "停用"
            return LoopToolResult("已标出", kind="page", dispatch="acknowledged", marked="停用")
        self.ex._invoke_tool = highlight
        out = self.run_request()
        self.assertEqual((out.state, out.error_code, out.marked), ("completed", "action_unknown", "停用"))
        self.assertEqual(handed, [("highlight", {"index": 3})])
        self.assertEqual(self.ex.s.approvals.cards, [])
        self.assertEqual(self.ex.clicks, [("PUT", PREFIX + "update-status", {"id": 7, "status": 1})])
        self.assertIsNone(self.ex.native_submit.armed)
        self.assertNotIn("form_submit", [a for a, _ in self.ex.dispatched])
        self.assertIn("助手尚未提交", out.feedback)

    def test_unknown_action_does_not_claim_a_refused_handoff(self):
        from web_operation_agent.execution.model_loop_contracts import LoopToolResult
        self.ex.native_submit.row_request = lambda *a: None
        self.ex.turn["marked"] = "旧控件"
        self.ex._invoke_tool = lambda *a: LoopToolResult("目标未核验", kind="page", state="refused",
                                                         error_code="invalid_target")
        out = self.run_request()
        self.assertEqual((out.state, out.error_code, out.marked), ("refused", "action_unknown", ""))
        self.assertNotIn("已标出", out.feedback)
        self.assertEqual(self.ex.s.events[-1][1]["handoff_error_code"], "invalid_target")

    def test_unknown_dangerous_action_needs_confirmation_before_handoff(self):
        self.ex.native_submit.row_request = lambda *a: None
        self.ex.native_submit.dangerous_ = True
        self.ex._invoke_tool = lambda *a: self.fail("must not highlight before confirmation")
        out = self.run_request()
        self.assertEqual(out.error_code, "dangerous_unconfirmed")

    def test_unknown_action_cannot_reuse_an_old_mark_after_an_unproved_completion(self):
        from web_operation_agent.execution.model_loop_contracts import LoopToolResult
        self.ex.native_submit.row_request = lambda *a: None
        self.ex.turn["marked"] = "旧控件"
        self.ex._invoke_tool = lambda *a: LoopToolResult("普通页面反馈", kind="page")
        out = self.run_request()
        self.assertEqual((out.state, out.error_code, out.marked), ("unverified", "action_unknown", ""))
        self.assertNotIn("已标出", out.feedback)

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


def receipt(action="fill", ref="e1", value="", observation=None):
    """A dispatched fill/select receipt as _invoke_tool would hand note_entries."""
    return NS(action=action, ok=True,
              args_echo={"ref": ref, "value": value, "option_label": value},
              observation=observation)


class EntryPlacementTests(_Setup, unittest.TestCase):
    def test_a_title_named_line_cell_fill_is_source_checked(self):
        self.ex.turn["source_ledger"] = SourceLedger()
        line = ElementRef("e9", "spinbutton", "", "input", 1,
                          {"form_scope": "dialog:编辑", "form_instance": "i1",
                           "title": "明细第1行·本次付款"})
        obs = NS(elements=[line])
        self.ex.note_entries(self.ex.s, self.ex.turn, (("本次付款", "9.99"),), obs,
                             receipts=[receipt(ref="e9", value="9.99")])
        entries = self.ex.turn["source_ledger"].entries
        self.assertEqual([(e.field, e.value, e.placed) for e in entries],
                         [("明细第1行·本次付款", "9.99", True)])
        self.assertEqual(self.run_request().error_code, "value_not_from_user")

    def test_an_unplaceable_fill_fails_closed(self):
        self.ex.turn["source_ledger"] = SourceLedger()
        self.ex.note_entries(self.ex.s, self.ex.turn, (("看不见的字段", "9.99"),),
                             NS(elements=[]))
        self.assertTrue(self.ex.turn["source_ledger"].entries)
        self.assertFalse(self.ex.turn["source_ledger"].entries[-1].placed)
        self.assertEqual(self.run_request().error_code, "value_not_from_user")

    def test_a_same_valued_query_fill_does_not_cover_the_business_fill(self):
        # A value written into a query/filter control covers only that fill: a
        # business field filled with the same value is still entered and checked.
        self.ex.turn["source_ledger"] = SourceLedger()
        e_query = ElementRef("e_q", "input", "单据号", "input", 9,
                             {"form_scope": "form:list", "page_control": "query"})
        e_business = ElementRef("e_b", "input", "", "input", 9,
                                {"form_scope": "form:crm/contact/update",
                                 "field_label": "备注"})
        obs = NS(elements=[e_query, e_business])
        self.ex.note_entries(self.ex.s, self.ex.turn,
                             (("单据号", "SYN-17"), ("备注", "SYN-17")), obs,
                             receipts=[receipt(ref="e_q", value="SYN-17")])
        entries = self.ex.turn["source_ledger"].entries
        self.assertEqual([(e.field, e.value, e.placed) for e in entries],
                         [("备注", "SYN-17", True)])
        self.assertEqual(self.run_request().error_code, "value_not_from_user")


class CandidateSourceTests(_Setup, unittest.TestCase):
    def _entry(self, value="13911112222", kind="text"):
        from web_operation_agent.execution.value_source import Entry
        entry = Entry("手机", value, kind=kind)
        self.ex.turn["source_ledger"].add(entry)
        return entry

    def test_a_candidate_read_after_the_fill_does_not_count(self):
        self._entry()
        from web_operation_agent.execution.value_source import check_entries
        self.ex.turn["source_ledger"].add_candidates([("手机", "13911112222")])
        verdicts = check_entries(self.ex.turn["source_ledger"], [])
        self.assertFalse(verdicts[0].ok)

    def test_a_candidate_cell_bound_to_another_field_is_refused(self):
        self._entry()
        from web_operation_agent.execution.value_source import check_entries
        self.ex.turn["source_ledger"].add_candidates([("别的字段", "13911112222")])
        verdicts = check_entries(self.ex.turn["source_ledger"], [])
        self.assertFalse(verdicts[0].ok)

    def test_a_record_name_sources_only_a_selection_field(self):
        self._entry()
        from web_operation_agent.execution.value_source import check_entries
        self.ex.turn["source_ledger"].add_candidates([("", "13911112222")])
        self.assertFalse(check_entries(self.ex.turn["source_ledger"], [])[0].ok)
        self.ex.turn["source_ledger"] = SourceLedger()
        self.ex.turn["source_ledger"].add_candidates([("", "13911112222")])
        self._entry(kind="select")
        self.assertTrue(check_entries(self.ex.turn["source_ledger"], [])[0].ok)


class ValueMeaningTests(_Setup, unittest.TestCase):
    def _verdict(self, value, words, label="手机"):
        from web_operation_agent.execution.value_source import Entry, check_entries
        ledger = SourceLedger()
        ledger.add(Entry(label, value))
        return check_entries(ledger, words)[0]

    def test_a_scaled_amount_does_not_also_source_the_base(self):
        self.assertTrue(self._verdict("3000", ["预付款3千"]).ok)
        self.assertFalse(self._verdict("3", ["预付款3千"]).ok)

    def test_a_period_word_without_a_clock_sources_no_timed_value(self):
        self.assertFalse(self._verdict("2026-11-20 00:00:00", ["改到明天下午"]).ok)
        self.assertTrue(self._verdict("2026-11-20 14:00:00", ["改到明天下午两点"]).ok)

    def test_a_date_receipt_cannot_source_a_timed_value(self):
        from web_operation_agent.execution.value_source import Entry, check_entries, note_receipt
        turn = {}
        note_receipt(turn, "", "2026-11-20")
        ledger = turn["source_ledger"]
        ledger.add(Entry("时间", "2026-11-20 00:00:00"))
        self.assertFalse(check_entries(ledger, ["改到明天下午"])[0].ok)
        turn = {}
        note_receipt(turn, "", "2026-11-20 14:00:00")
        turn["source_ledger"].add(Entry("时间", "2026-11-20 14:00:00"))
        self.assertTrue(check_entries(turn["source_ledger"], [])[0].ok)

    def test_a_date_range_needs_both_ends_sourced(self):
        self.assertTrue(self._verdict("2026-11-20 ~ 2026-11-22",
                                      ["从11月20号到11月22号"]).ok)
        self.assertFalse(self._verdict("2026-11-20 ~ 2026-11-22",
                                       ["从11月20号开始"]).ok)

    def test_a_fieldless_receipt_binds_the_first_entry_only(self):
        from web_operation_agent.execution.value_source import Entry, check_entries, note_receipt
        turn = {}
        note_receipt(turn, "", "13911112222")
        ledger = turn["source_ledger"]
        ledger.add(Entry("手机", "13911112222"))
        ledger.add(Entry("备用手机", "13911112222"))
        verdicts = check_entries(ledger, [])
        self.assertEqual([v.ok for v in verdicts], [True, False])


class HandoffWaitTests(_Setup, unittest.TestCase):
    """After a handoff marks a control, the turn waits (outside its clock) for the
    person's own press, then reads the record back with the same actor."""

    def arm_wait(self, event=None, mark=0):
        from contextlib import contextmanager

        @contextmanager
        def waiting():
            yield

        self.ex.s.approvals.awaiting_person = waiting
        self.ex.s.cancelled = lambda: False
        self.ex.native_submit.user_write_mark = lambda: mark
        self.ex.native_submit.user_write_event = lambda *a, **kw: event
        self.ex.turn["submit_targets"] = {("dialog:编辑", "i1"): {
            "kind": "form", "mode": "update", "prefixes": [PREFIX],
            "record_id": "7", "baseline": deepcopy(LOADED)}}
        self.ex.turn["handoff_target_key"] = ("dialog:编辑", "i1")
        self.ex.turn["handoff_button"] = "保存"

    def test_a_press_is_seen_and_the_change_is_read_back(self):
        self.arm_wait(event={"method": "PUT", "path": PREFIX + "update",
                             "status": 200, "record_id": "7"})
        self.ex.native_submit.record = {**LOADED, "mobile": "13900000000"}
        note = self.ex._await_handoff_press(self.ex.s, self.ex.turn)
        self.assertIn("已接受", note)
        self.assertIn("mobile：13800000000→13900000000", note)
        kinds = [e for e, _ in self.ex.s.events]
        self.assertEqual(kinds[-2:], ["handoff_wait", "handoff_wait_result"])

    def test_no_press_by_the_limit_says_so(self):
        self.arm_wait(event=None)
        note = self.ex._await_handoff_press(self.ex.s, self.ex.turn)
        self.assertIn("没有看到你点击", note)
        self.assertIn("表单未提交", note)

    def test_a_press_without_a_readback_still_reports_acceptance(self):
        self.arm_wait(event={"method": "PUT", "path": PREFIX + "update",
                             "status": 200, "record_id": "7"})
        self.ex.native_submit.exists = False
        note = self.ex._await_handoff_press(self.ex.s, self.ex.turn)
        self.assertIn("已接受", note)
        self.assertIn("回读", note)

    def test_without_a_marked_submit_control_there_is_no_wait(self):
        self.arm_wait(event={"method": "PUT", "path": PREFIX + "update", "status": 200})
        self.ex.turn.pop("handoff_button")
        self.assertEqual(self.ex._await_handoff_press(self.ex.s, self.ex.turn), "")

    def test_a_cancelled_turn_stops_waiting(self):
        self.arm_wait(event=None)
        self.ex.s.cancelled = lambda: True
        self.assertEqual(self.ex._await_handoff_press(self.ex.s, self.ex.turn), "")

    def test_a_create_handoff_waits_only_for_the_create_interface(self):
        seen = []

        def event_fn(prefixes, record_id, mark, **kw):
            seen.append(kw.get("create"))
            return {"method": "POST", "path": PREFIX + "create", "status": 200,
                    "record_id": "9"}
        self.arm_wait(mark=0)
        self.ex.native_submit.user_write_event = event_fn
        self.ex.turn["submit_targets"][("dialog:编辑", "i1")] = {
            "kind": "form", "mode": "create", "prefixes": [PREFIX], "record_id": ""}
        self.ex.native_submit.record = {**LOADED, "id": 9}
        note = self.ex._await_handoff_press(self.ex.s, self.ex.turn)
        self.assertEqual(seen, [True])
        self.assertIn("已接受", note)


if __name__ == "__main__":
    unittest.main()
