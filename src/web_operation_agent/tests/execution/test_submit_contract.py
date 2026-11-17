"""The approval contract: complete cards, requests that match them, one effect once."""
from datetime import timedelta, timezone
import json
import unittest

from web_operation_agent.execution.submit_contract import (
    ActionRequest, ContractRefused, FormData, SubmitConventions, SubmitPermit,
    build_card, diff_models, same_value, terminal_state)


def _decode(status, body):
    """The synthetic application's envelope: {code: 0, data: <id>}, refused otherwise."""
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

PREFIX = "/admin-api/crm/contact/"
CATALOG = frozenset({("POST", PREFIX + "create"), ("PUT", PREFIX + "update"),
                     ("PUT", PREFIX + "update-status"), ("POST", PREFIX + "create-line")})
LOADED = {"id": 7, "name": "张三", "mobile": "13800000000", "customerId": 3, "ownerUserId": 1,
          "status": 0, "lines": [{"id": 1, "sku": "A", "qty": 2}, {"id": 2, "sku": "B", "qty": 1}]}


def form(model, mode="update", record="7"):
    return FormData.from_json({"model": model, "mode": mode, "record_id": record,
        "items": [{"prop": "customerId", "label": "客户", "display": "Acme"},
                  {"prop": "mobile", "label": "手机"}, {"prop": "name", "label": "姓名"}]})


def update_card(**changes):
    model = {**LOADED, **changes}
    return build_card(conventions=CONVENTIONS, loaded=form(LOADED), form=form(model), page="/crm/contact", button="保存",
                      mode="update", record_id="7", target_name="张三")


def permit(card, baseline=None, reread=None, **kw):
    return SubmitPermit("a" * 64, card, (PREFIX,), CATALOG, baseline if baseline is not None else LOADED,
                        reread, conventions=CONVENTIONS, **kw)


def body(**changes):
    return json.dumps({**LOADED, **changes}).encode()


class DiffTests(unittest.TestCase):
    def test_hidden_related_and_line_changes_are_all_found(self):
        after = {**LOADED, "mobile": "13900000000", "customerId": 4, "ownerUserId": 2,
                 "lines": [{"id": 1, "sku": "A", "qty": 3}, {"id": 3, "sku": "C", "qty": 1}]}
        changes = {"/".join(map(str, c.path)) for c in diff_models(LOADED, after, conventions=CONVENTIONS)}
        self.assertEqual(changes, {"mobile", "customerId", "ownerUserId", "lines/#1/qty",
                                   "lines/#2", "lines/#3", "lines/#3/id" if False else "lines/#3"})

    def test_null_and_blank_and_numeric_text_do_not_differ(self):
        self.assertTrue(same_value({"a": None, "b": "5", "c": ""}, {"a": "", "b": 5.0, "c": None}))
        self.assertFalse(same_value({"a": 1}, {"a": 2}))

    def test_timestamp_and_date_text_are_one_value_in_the_declared_zone(self):
        self.assertTrue(same_value("2026-11-12", 1794412800000,
                                   zone=CONVENTIONS.epoch_zone))
        self.assertFalse(same_value("2026-11-13", 1794412800000,
                                    zone=CONVENTIONS.epoch_zone))
        self.assertFalse(same_value("2026-11-12", 1794412800000))


class CardTests(unittest.TestCase):
    def test_update_card_lists_every_change_with_labels(self):
        card = update_card(mobile="13900000000", customerId=4)
        text = {d.label: d.text for d in card.details()}
        self.assertEqual(text["手机"], "13800000000 → 13900000000")
        self.assertIn("客户", text)
        self.assertIn("编号 7", card.target)
        self.assertEqual(card.proposal().tool, "native_form_update")

    def test_changes_the_person_did_not_ask_for_are_listed_apart(self):
        card = build_card(conventions=CONVENTIONS, loaded=form(LOADED), form=form({**LOADED, "mobile": "1", "ownerUserId": 9}),
                          page="/crm/contact", button="保存", mode="update", record_id="7",
                          requested=frozenset({"mobile"}))
        labels = [d.label for d in card.details()]
        self.assertTrue(any(label.startswith("用户没有要求") for label in labels))
        self.assertEqual({c.field for c in card.changes}, {"mobile"})

    def test_create_card_marks_form_defaults(self):
        loaded = form({"status": 0, "name": ""}, "create", "")
        card = build_card(conventions=CONVENTIONS, loaded=loaded, form=form({"status": 0, "name": "新客户", "level": "A"}, "create", ""),
                          page="/crm/customer", button="确定", mode="create")
        self.assertEqual({c.field for c in card.other}, {"status"})
        self.assertEqual({c.field for c in card.changes}, {"name", "level"})
        self.assertEqual(card.proposal().tool, "native_form_create")

    def test_no_change_and_unreadable_forms_are_refused(self):
        with self.assertRaises(ContractRefused) as nothing:
            update_card()
        self.assertEqual(nothing.exception.code, "no_change")
        with self.assertRaises(ContractRefused):
            FormData.from_json({"model": {"x": object()}, "mode": "update"})
        with self.assertRaises(ContractRefused):
            FormData.from_json(None)


class PermitTests(unittest.TestCase):
    def test_matching_update_is_admitted_once_and_completes(self):
        permit_ = permit(update_card(mobile="13900000000"), reread=lambda: LOADED)
        first = permit_.admit("PUT", PREFIX + "update", "", body(mobile="13900000000"))
        self.assertTrue(first.allowed)
        permit_.settle(first.sequence, 200)
        self.assertTrue(permit_.complete())
        self.assertEqual(terminal_state(permit_, readback_ok=True), "completed")

    def test_second_identical_request_is_a_duplicate_attempt(self):
        permit_ = permit(update_card(mobile="1"), reread=lambda: LOADED)
        self.assertTrue(permit_.admit("PUT", PREFIX + "update", "", body(mobile="1")).allowed)
        again = permit_.admit("PUT", PREFIX + "update", "", body(mobile="1"))
        self.assertFalse(again.allowed)
        self.assertEqual((again.code, permit_.duplicates), ("duplicate", 1))

    def test_other_record_other_interface_and_foreign_prefix_are_refused(self):
        card = update_card(mobile="1")
        permit_ = permit(card, reread=lambda: LOADED)
        self.assertEqual(permit_.admit("PUT", PREFIX + "update", "",
                                       json.dumps({**LOADED, "id": 8, "mobile": "1"}).encode()).code,
                         "target_mismatch")
        self.assertEqual(permit_.admit("PUT", "/admin-api/crm/customer/update", "", body(mobile="1")).code,
                         "interface_not_for_target")
        self.assertEqual(permit_.admit("DELETE", PREFIX + "delete", "id=7", b"").code,
                         "interface_not_for_target")
        self.assertEqual(permit_.allowed(), [])
        self.assertEqual(terminal_state(permit_, readback_ok=None), "not_executed")

    def test_body_that_is_not_the_form_is_refused(self):
        permit_ = permit(update_card(mobile="1"), reread=lambda: LOADED)
        self.assertEqual(permit_.admit("PUT", PREFIX + "update", "", body(mobile="2")).code,
                         "body_differs_from_form")

    def test_a_change_not_on_the_card_is_refused(self):
        card = update_card(mobile="1")
        permit_ = permit(card, baseline={**LOADED, "ownerUserId": 5}, reread=lambda: {**LOADED, "ownerUserId": 5})
        # The record already differs from the form in a field the card does not carry.
        self.assertEqual(permit_.admit("PUT", PREFIX + "update", "", body(mobile="1")).code,
                         "change_not_on_card")

    def test_record_changed_after_the_card_blocks_the_request(self):
        permit_ = permit(update_card(mobile="1"), reread=lambda: {**LOADED, "ownerUserId": 99})
        self.assertEqual(permit_.admit("PUT", PREFIX + "update", "", body(mobile="1")).code, "record_changed")

    def test_unreadable_record_blocks_the_request(self):
        def boom():
            raise OSError("down")
        permit_ = permit(update_card(mobile="1"), reread=boom)
        self.assertEqual(permit_.admit("PUT", PREFIX + "update", "", body(mobile="1")).code, "record_unreadable")

    def test_volatile_fields_do_not_count_as_a_change(self):
        permit_ = permit(update_card(mobile="1"), reread=lambda: {**LOADED, "updateTime": 5})
        self.assertTrue(permit_.admit("PUT", PREFIX + "update", "", body(mobile="1")).allowed)

    def test_create_admits_no_target_and_matches_form(self):
        loaded = form({"name": ""}, "create", "")
        card = build_card(conventions=CONVENTIONS, loaded=loaded, form=form({"name": "新", "level": "A"}, "create", ""),
                          page="/crm/customer", button="确定", mode="create")
        permit_ = SubmitPermit("a" * 64, card, (PREFIX,), CATALOG, conventions=CONVENTIONS)
        self.assertTrue(permit_.admit("POST", PREFIX + "create", "",
                                      json.dumps({"name": "新", "level": "A"}).encode()).allowed)
        self.assertTrue(permit_.complete())
        self.assertFalse(SubmitPermit("a" * 64, card, (PREFIX,), CATALOG, conventions=CONVENTIONS).admit(
            "POST", PREFIX + "create", "", json.dumps({"id": 5, "name": "新", "level": "A"}).encode()).allowed)

    def test_double_create_is_released_once(self):
        loaded = form({"name": ""}, "create", "")
        card = build_card(conventions=CONVENTIONS, loaded=loaded, form=form({"name": "新"}, "create", ""), page="p", button="b", mode="create")
        permit_ = SubmitPermit("a" * 64, card, (PREFIX,), CATALOG, conventions=CONVENTIONS)
        payload = json.dumps({"name": "新"}).encode()
        results = [permit_.admit("POST", PREFIX + "create", "", payload).allowed for _ in range(2)]
        self.assertEqual(results, [True, False])
        self.assertEqual(permit_.duplicates, 1)

    def test_row_action_needs_the_target_and_one_documented_interface(self):
        card = build_card(conventions=CONVENTIONS, loaded=None, form=None, page="/crm/contact", button="停用", mode="action",
                          target_name="张三", record_id="7", record=LOADED,
                          action_request=ActionRequest("PUT", PREFIX + "update-status", {"id": 7, "status": 1}))
        permit_ = SubmitPermit("a" * 64, card, (PREFIX,), CATALOG, LOADED, lambda: LOADED,
                     conventions=CONVENTIONS)
        self.assertFalse(permit_.admit("PUT", PREFIX + "update-status", "",
                                       json.dumps({"id": 8, "status": 1}).encode()).allowed)
        self.assertTrue(permit_.admit("PUT", PREFIX + "update-status", "",
                                      json.dumps({"id": 7, "status": 1}).encode()).allowed)

    def test_same_entity_other_action_or_extra_field_is_never_this_card(self):
        card = build_card(conventions=CONVENTIONS, loaded=None, form=None, page="p", button="停用", mode="action", record_id="7",
            record=LOADED, action_request=ActionRequest("PUT", PREFIX + "update-status", {"id": 7, "status": 1}))
        for path, payload, code in [
            (PREFIX + "update", {"id": 7, "status": 1}, "action_not_on_card"),
            (PREFIX + "update-status", {"id": 8, "status": 1}, "target_mismatch"),
            (PREFIX + "update-status", {"id": 7, "status": 0}, "action_body_not_on_card"),
            (PREFIX + "update-status", {"id": 7, "status": 1, "name": "extra"}, "action_body_not_on_card")]:
            p = SubmitPermit("a" * 64, card, (PREFIX,), CATALOG, LOADED, lambda: LOADED,
                     conventions=CONVENTIONS)
            self.assertEqual(p.admit("PUT", path, "", json.dumps(payload).encode()).code, code)
        unbound = build_card(conventions=CONVENTIONS, loaded=None, form=None, page="p", button="停用", mode="action", record_id="7")
        self.assertFalse(SubmitPermit("a" * 64, unbound, (PREFIX,), CATALOG, conventions=CONVENTIONS).admit("PUT", PREFIX + "update-status", "", b'{"id":7,"status":1}').allowed)

    def test_empty_extra_fields_are_not_part_of_an_action_or_form_payload(self):
        for empty in (None, "", [], {}):
            card = build_card(conventions=CONVENTIONS, loaded=None, form=None, page="p", button="停用", mode="action", record_id="7",
                record=LOADED, action_request=ActionRequest("PUT", PREFIX + "update-status", {"id": 7, "status": 1}))
            p = SubmitPermit("a" * 64, card, (PREFIX,), CATALOG, LOADED, lambda: LOADED,
                     conventions=CONVENTIONS)
            self.assertEqual(p.admit("PUT", PREFIX + "update-status", "", json.dumps({"id": 7, "status": 1, "extra": empty}).encode()).code,
                             "action_body_not_on_card")
            p = permit(update_card(mobile="1"))
            self.assertEqual(p.admit("PUT", PREFIX + "update", "", body(mobile="1", extra=empty)).code,
                             "body_differs_from_form")
        from web_operation_agent.execution.submit_contract import same_payload
        self.assertFalse(same_payload({"line": {"qty": 1, "extra": None}}, {"line": {"qty": 1}}))

    def test_restoration_notes_count_toward_the_card_limit(self):
        from web_operation_agent.execution.submit_contract import Change
        with self.assertRaises(ContractRefused) as raised:
            build_card(conventions=CONVENTIONS, loaded=form(LOADED), form=form({**LOADED, "mobile": "1"}), page="p", button="保存",
                mode="update", record_id="7", restored=tuple(Change((f"f{i}",), 1, 0) for i in range(151)))
        self.assertEqual(raised.exception.code, "card_too_large")

    def test_expired_permit_admits_nothing(self):
        now = [0.0]
        permit_ = permit(update_card(mobile="1"), reread=lambda: LOADED, ttl_s=5, clock=lambda: now[0])
        now[0] = 6.0
        self.assertEqual(permit_.admit("PUT", PREFIX + "update", "", body(mobile="1")).code, "permit_expired")


class LinesTests(unittest.TestCase):
    def test_lines_are_compared_with_the_form_as_loaded_not_the_records_own_shape(self):
        record = {**LOADED, "lines": [{"id": 1, "sku": "A", "qty": 2, "recordOnly": 1}, {"id": 2, "sku": "B", "qty": 1, "recordOnly": 1}]}
        changed = [{"id": 1, "sku": "A", "qty": 3}, {"id": 2, "sku": "B", "qty": 1}]
        card = update_card(lines=changed)
        self.assertEqual(card.changed_fields, {"lines"})
        permit_ = permit(card, baseline=record, reread=lambda: record)
        self.assertTrue(permit_.admit("PUT", PREFIX + "update", "", body(lines=changed)).allowed)
        # lines the card does not show are refused even though the record's shape differs
        other = SubmitPermit("a" * 64, update_card(mobile="1"), (PREFIX,), CATALOG, record, lambda: record,
                      conventions=CONVENTIONS)
        sent = {**LOADED, "mobile": "1", "lines": changed}
        self.assertEqual(other.admit("PUT", PREFIX + "update", "", json.dumps(sent).encode()).code, "body_differs_from_form")

    def test_an_unchanged_form_with_lines_does_not_trip_on_the_records_shape(self):
        record = {**LOADED, "lines": [{"id": 1, "sku": "A", "qty": 2, "recordOnly": 1}, {"id": 2, "sku": "B", "qty": 1, "recordOnly": 1}]}
        permit_ = permit(update_card(mobile="1"), baseline=record, reread=lambda: record)
        self.assertTrue(permit_.admit("PUT", PREFIX + "update", "", body(mobile="1")).allowed)


    def test_a_record_that_shapes_a_value_differently_from_the_form_is_not_a_change(self):
        record = {**LOADED, "accountId": None}
        loaded = {**LOADED, "accountId": 0}
        card = build_card(conventions=CONVENTIONS, loaded=form(loaded), form=form({**loaded, "mobile": "1"}), page="p", button="保存", mode="update", record_id="7")
        permit_ = SubmitPermit("a" * 64, card, (PREFIX,), CATALOG, record, lambda: record,
                               conventions=CONVENTIONS)
        sent = json.dumps({**loaded, "mobile": "1"}).encode()
        self.assertTrue(permit_.admit("PUT", PREFIX + "update", "", sent).allowed)


class ReviewRegressionTests(unittest.TestCase):
    def test_a_query_id_that_differs_from_the_body_id_or_extra_query_keys_are_refused(self):
        card = build_card(conventions=CONVENTIONS, loaded=None, form=None, page="p", button="停用", mode="action", record_id="7", record=LOADED,
                          action_request=ActionRequest("PUT", PREFIX + "update-status", {"status": 1}))
        permit_ = SubmitPermit("a" * 64, card, (PREFIX,), CATALOG, LOADED, lambda: LOADED,
                     conventions=CONVENTIONS)
        self.assertEqual(permit_.admit("PUT", PREFIX + "update-status", "id=999", b'{"id": 7, "status": 1}').code, "target_mismatch")
        self.assertEqual(permit_.admit("PUT", PREFIX + "update-status", "id=7&force=1", b'{"id": 7}').code, "query_not_on_card")
        self.assertTrue(permit_.admit("PUT", PREFIX + "update-status", "id=7", b'{"status": 1}').allowed)

    def test_text_that_differs_only_in_leading_zeros_is_a_change_but_a_number_held_as_text_is_not(self):
        self.assertFalse(same_value("007", "7"))
        self.assertFalse(same_value("0012.50", "12.5"))
        self.assertTrue(same_value("5", 5) and same_value(12.5, "12.5") and same_value("", None))
        code = diff_models({"code": "0012"}, {"code": "12"}, conventions=CONVENTIONS)
        self.assertEqual([c.field for c in code], ["code"])

    def test_a_second_request_of_a_two_part_update_is_not_refused_for_the_first_ones_own_effect(self):
        card = update_card(mobile="1", customerId=4)
        record = dict(LOADED)
        permit_ = permit(card, baseline=dict(record), reread=lambda: dict(record))
        first = permit_.admit("PUT", PREFIX + "update", "", json.dumps({"id": 7, "mobile": "1"}).encode())
        self.assertTrue(first.allowed)
        record["mobile"] = "1"                                   # the application applied it
        second = permit_.admit("PUT", PREFIX + "update", "", json.dumps({"id": 7, "customerId": 4}).encode())
        self.assertTrue(second.allowed, second.code)
        record["ownerUserId"] = 99                               # someone else, unrelated to what was released
        third = permit_.admit("PUT", PREFIX + "update-status", "", json.dumps({"id": 7, "mobile": "1", "status": 0}).encode())
        self.assertEqual(third.code, "record_changed")

    def test_a_create_is_one_effect_whatever_else_differs_in_the_second_body(self):
        loaded = form({"name": ""}, "create", "")
        card = build_card(conventions=CONVENTIONS, loaded=loaded, form=form({"name": "新"}, "create", ""), page="p", button="b", mode="create")
        permit_ = SubmitPermit("a" * 64, card, (PREFIX,), CATALOG, conventions=CONVENTIONS)
        bodies = [{"name": "新"}, {"name": "新", "remark": ""}, {"name": "新", "a": None}]
        decisions = [permit_.admit("POST", PREFIX + "create", "", json.dumps(b).encode()) for b in bodies]
        self.assertEqual([d.allowed for d in decisions], [True, False, False])
        self.assertEqual(permit_.duplicates, 2)

    def test_a_write_that_carries_nothing_the_card_shows_is_refused_whatever_its_interface(self):
        permit_ = permit(update_card(mobile="1"), reread=lambda: LOADED)
        self.assertEqual(permit_.admit("DELETE", PREFIX + "delete", "", b'{"id": 7}').code, "interface_not_for_target")
        self.assertEqual(permit_.admit("PUT", PREFIX + "update-status", "", b'{"id": 7}').code, "request_without_card_change")

    def test_the_record_overwritten_by_a_plain_value_is_refused_but_a_null_for_zero_is_not(self):
        card = update_card(mobile="1")
        record = {**LOADED, "ownerUserId": 5}
        self.assertEqual(permit(card, baseline=record, reread=lambda: record).admit(
            "PUT", PREFIX + "update", "", body(mobile="1")).code, "change_not_on_card")

    def test_binary64_float_tail_noise_matches_precision_15_without_relaxing_real_deltas(self):
        self.assertTrue(same_value(1370.13 + 1381.43, 2751.56))
        self.assertTrue(same_value(0.1 + 0.2, 0.3))
        self.assertFalse(same_value(2751.560001, 2751.56))
        self.assertFalse(same_value(2751.57, 2751.56))
        self.assertFalse(same_value("007", "7"))
        self.assertFalse(same_value("0012.50", "12.5"))

        base_with_total = {**LOADED, "totalPrice": 2751.56}
        card_model = {**base_with_total, "mobile": "13900000000"}
        card = build_card(
            conventions=CONVENTIONS,
            loaded=form(base_with_total),
            form=form(card_model),
            page="/erp/purchase-in",
            button="确定",
            mode="update",
            record_id="7",
            target_name="张三",
        )
        permit_ = SubmitPermit("a" * 64, card, (PREFIX,), CATALOG, base_with_total, lambda: dict(base_with_total),
                               conventions=CONVENTIONS)
        sent = json.dumps({**card_model, "totalPrice": 1370.13 + 1381.43}).encode()
        self.assertTrue(permit_.admit("PUT", PREFIX + "update", "", sent).allowed)


class DryRunTests(unittest.TestCase):
    def test_a_dry_run_judges_like_a_real_one_and_sends_nothing(self):
        permit_ = permit(update_card(mobile="1"), reread=lambda: LOADED, dry_run=True)
        decision = permit_.admit("PUT", PREFIX + "update", "", body(mobile="1"))
        self.assertEqual((decision.allowed, decision.code), (False, "dry_run"))
        self.assertEqual([r.decision for r in permit_.captured()], ["would_allow"])
        self.assertEqual(permit_.allowed(), [])
        self.assertTrue(permit_.complete())
        self.assertEqual(permit_.admit("PUT", PREFIX + "update", "", body(mobile="9")).code, "body_differs_from_form")


class TerminalTests(unittest.TestCase):
    def _two_part(self, second):
        card = update_card(mobile="1", customerId=4)
        permit_ = permit(card, reread=lambda: LOADED)
        first = permit_.admit("PUT", PREFIX + "update", "",
                              json.dumps({"id": 7, "mobile": "1"}).encode())
        permit_.settle(first.sequence, 200)
        second_ = permit_.admit("PUT", PREFIX + "update", "",
                                json.dumps({"id": 7, "customerId": 4}).encode())
        permit_.settle(second_.sequence, second)
        return permit_

    def test_two_requests_each_released_once_complete(self):
        self.assertEqual(terminal_state(self._two_part(200), readback_ok=True), "completed")

    def test_second_rejected_is_partial(self):
        self.assertEqual(terminal_state(self._two_part(400), readback_ok=True), "partial")

    def test_unanswered_request_is_indeterminate_and_all_rejected_is_failed(self):
        self.assertEqual(terminal_state(self._two_part(None), readback_ok=None), "indeterminate")
        permit_ = permit(update_card(mobile="1"), reread=lambda: LOADED)
        sent = permit_.admit("PUT", PREFIX + "update", "", body(mobile="1"))
        permit_.settle(sent.sequence, 500)
        self.assertEqual(terminal_state(permit_, readback_ok=False), "failed")

    def test_stray_blocked_request_after_success_is_partial(self):
        permit_ = permit(update_card(mobile="1"), reread=lambda: LOADED)
        sent = permit_.admit("PUT", PREFIX + "update", "", body(mobile="1"))
        permit_.settle(sent.sequence, 200)
        permit_.admit("POST", PREFIX + "create-line", "", b"{}")
        self.assertEqual(terminal_state(permit_, readback_ok=True), "partial")


if __name__ == "__main__":
    unittest.main()
