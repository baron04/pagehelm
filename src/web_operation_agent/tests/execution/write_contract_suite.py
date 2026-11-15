"""The native-write contract run the same way over each application's own declarations.

Each adapter's test file subclasses `SharedSubmitContractSuite` and supplies
`make_fixture` — a `Fixture` built from its real `SubmitPort` over a synthetic backend,
its record and request shapes, and its accepted-write journal. Every identity key,
volatile key, prefix and response envelope below comes from the fixture's declared
`SubmitConventions` and port; the suite itself names none.
"""
from __future__ import annotations

import json
import unittest
from dataclasses import dataclass
from typing import Callable

from web_operation_agent.execution.native_submit import (opening_values,
                                                         _readback_changes)
from web_operation_agent.execution.submit_contract import (ContractRefused, FormData,
                                                           SubmitConventions,
                                                           SubmitPermit, build_card,
                                                           terminal_state)


def _form(model, mode="update", record_id=""):
    return FormData.from_json({"model": model, "mode": mode,
                               "record_id": record_id, "title": "f", "items": []})


@dataclass(frozen=True)
class Fixture:
    """One application's side of the shared scenarios."""
    conv: SubmitConventions
    port: object                    # the adapter's SubmitPort over its synthetic backend
    prefixes: tuple[str, ...]       # what the port binds for this record
    catalog: frozenset              # the port's documented writes under `prefixes`
    record_id: str
    record: dict                    # the record as the same actor reads it
    loaded_model: dict              # the form at open, in submission shape
    form_model: dict                # what the card submits: one field and one line changed
    field: str                      # the changed top-level field
    line_identity: str              # a line's identity under the declared line key
    line_field: str                 # the changed field inside that line
    update_method: str              # the carded update request, in the app's shape
    update_path: str
    update_query: str
    update_body: dict
    foreign_path: str               # a documented-looking write outside the card's catalog
    create_method: str              # the carded create request
    create_path: str
    create_body: dict
    create_response: bytes          # the accepted create envelope naming `new_id`
    new_id: str
    readback: dict                  # the record as the port reads it after the press
    user_write: Callable            # record one accepted user write into the journal
    restorable: tuple               # (record, loaded, current): the page's self-edit undoes
    restored: dict                  # what opening_values must produce for it
    unrestorable: tuple             # (record, loaded, current): must hard-stop


class SharedSubmitContractSuite:
    """Approval card, request-to-card release, self-edit restore, handoff, readback —
    identical for every application, through its own declared conventions.

    A mixin: each adapter's test class pairs it with `unittest.TestCase`, so the base
    itself is never collected where it is imported."""

    def make_fixture(self) -> Fixture:
        raise NotImplementedError

    def setUp(self):
        self.fx = self.make_fixture()

    def _update_card(self):
        fx = self.fx
        return build_card(conventions=fx.conv, loaded=_form(fx.loaded_model),
                          form=_form(fx.form_model), page="记录", button="保存",
                          mode="update", record_id=fx.record_id, record=fx.record)

    def _permit(self, card, baseline=None):
        fx = self.fx
        return SubmitPermit(actor="person", card=card, prefixes=fx.prefixes,
                            catalog=fx.catalog, conventions=fx.conv,
                            baseline=baseline if baseline is not None else fx.record,
                            reread=lambda: fx.record)

    # -- the approval card ---------------------------------------------------------------
    def test_the_update_card_lists_changes_by_the_declared_identities(self):
        card = self._update_card()
        paths = {"/".join(str(p) for p in change.path) for change in card.changes}
        self.assertIn(self.fx.field, paths)
        self.assertIn(f"lines/#{self.fx.line_identity}/{self.fx.line_field}", paths)
        self.assertEqual(self.fx.record_id, card.record_id)

    # -- request-to-card permit matching ----------------------------------------------------
    def test_the_permit_releases_exactly_the_carded_request_once(self):
        fx, permit = self.fx, self._permit(self._update_card())
        body = json.dumps(fx.update_body).encode()
        first = permit.admit(fx.update_method, fx.update_path, fx.update_query, body)
        self.assertTrue(first.allowed, first.code)
        self.assertEqual("duplicate", permit.admit(fx.update_method, fx.update_path,
                                                   fx.update_query, body).code)

    def test_a_write_outside_the_card_never_releases(self):
        fx, permit = self.fx, self._permit(self._update_card())
        wrong_target = json.dumps({**fx.update_body,
                                   fx.conv.record_key: "other-record"}).encode()
        self.assertEqual("target_mismatch",
                         permit.admit(fx.update_method, fx.update_path,
                                      fx.update_query, wrong_target).code)
        self.assertEqual("interface_not_for_target",
                         permit.admit(fx.update_method, fx.foreign_path, "", b"{}").code)
        self.assertEqual("query_not_on_card",
                         permit.admit(fx.update_method, fx.update_path,
                                      fx.update_query + "&extra=1" if fx.update_query
                                      else "extra=1",
                                      json.dumps(fx.update_body).encode()).code)
        self.assertEqual("not_executed", self._terminal(permit))

    def _terminal(self, permit):
        return terminal_state(permit, readback_ok=True)

    def test_the_carded_create_releases_once_and_names_the_new_record(self):
        fx = self.fx
        card = build_card(conventions=fx.conv, loaded=_form({}, mode="create"),
                          form=_form(fx.create_body, mode="create"), page="记录",
                          button="保存", mode="create")
        permit = self._permit(card, baseline=None)
        body = json.dumps(fx.create_body).encode()
        first = permit.admit(fx.create_method, fx.create_path, "", body)
        self.assertTrue(first.allowed, first.code)
        self.assertEqual("duplicate", permit.admit(fx.create_method, fx.create_path,
                                                   "", body).code)
        permit.settle(first.sequence, 200, fx.create_response)
        self.assertEqual(fx.new_id, permit.created_id())

    # -- the page's own opening edits ---------------------------------------------------------
    def test_a_restorable_opening_self_edit_is_undone(self):
        record, loaded, current = self.fx.restorable
        self.assertEqual(self.fx.restored,
                         opening_values(record, loaded, current, self.fx.conv))

    def test_an_unrestorable_opening_self_edit_hard_stops(self):
        record, loaded, current = self.fx.unrestorable
        with self.assertRaises(ContractRefused) as held:
            opening_values(record, loaded, current, self.fx.conv)
        self.assertEqual("opening_changes", held.exception.code)

    # -- the person's press after a handoff ----------------------------------------------------
    def test_the_wait_wakes_on_the_persons_press_not_on_unrelated_writes(self):
        fx = self.fx
        mark = fx.port.user_write_mark()
        fx.user_write(fx.update_method, fx.update_path, record_id="other-record")
        self.assertIsNone(fx.port.user_write_event(fx.prefixes, fx.record_id, mark))
        fx.user_write(fx.update_method, fx.update_path, record_id=fx.record_id)
        event = fx.port.user_write_event(fx.prefixes, fx.record_id, mark)
        self.assertIsNotNone(event)
        self.assertEqual(fx.record_id, event["record_id"])

    def test_a_create_handoff_answers_with_the_new_record_id(self):
        fx = self.fx
        mark = fx.port.user_write_mark()
        fx.user_write(fx.update_method, fx.update_path, record_id="other-record")
        self.assertIsNone(fx.port.user_write_event(fx.prefixes, "", mark, create=True))
        fx.user_write(fx.create_method, fx.create_path, created_id=fx.new_id)
        event = fx.port.user_write_event(fx.prefixes, "", mark, create=True)
        self.assertIsNotNone(event)
        self.assertEqual(fx.new_id, event["record_id"])

    # -- readback by the same actor --------------------------------------------------------------
    def test_the_same_actor_readback_reports_what_the_press_changed(self):
        fx = self.fx
        record = fx.port.read_record(fx.prefixes, fx.record_id)
        self.assertEqual(fx.readback, record)
        notes = _readback_changes(fx.record, record, zone=fx.conv.epoch_zone)
        self.assertTrue(any(note.startswith(f"{fx.field}：") for note in notes), notes)
