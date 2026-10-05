"""Opaque current-page entries must preserve admission, actor and receipt boundaries."""
from dataclasses import replace
import json
import time
import unittest
from unittest.mock import patch

from web_operation_agent.business.form_entries import FormEntryCandidate, FormEntryTools
from web_operation_agent.business.forms import ApplicationFormEntry
from web_operation_agent.business.errors import (BusinessToolRequestError, BusinessToolIdentityError,
                                               BusinessToolContractError, BusinessToolPermissionError)
from web_operation_agent.schemas import ElementRef, PageObservation, ActionReceipt
from web_operation_agent.state.identity import PrincipalContext, OperationContext, PrincipalError


def context(actor="writer", turn="t1"):
    now = int(time.time() * 1000)
    p = PrincipalContext(schema_version=1, application_id="fixture", tenant_id="1", workspace_id="1",
        actor_id=actor, delegation_id="d1", issued_at=now - 1000, expires_at=now + 60000,
        scopes=("forms:prepare",))
    return OperationContext.create(p, session_id="s1", turn_id=turn,
        operation_id="entry", deadline_ms=now + 30000, now_ms=now)


def page(epoch=1, ready="ready"):
    return PageObservation(1, epoch, "http://app.test/items", "Items", [],
        [ElementRef("e1", "button", "Prepare", "button", epoch, {})], render_state=ready,
        page_route="/items")


class Port:
    required_scopes = frozenset({"forms:prepare"})
    def __init__(self):
        self.accepted, self.released = 0, 0
    def form_entries(self, observation, *, context):
        return (FormEntryCandidate("private-native-reference", "Prepare", "create"),)
    def plan_entry(self, reference, observation, *, context):
        return ApplicationFormEntry(reference, observation.elements[0])
    def accept_entry(self, entry, before, receipt, observation, *, context):
        self.accepted += 1
        return "current-private-form"
    def discard_entry(self, entry, *, context):
        self.released += 1


class FormEntriesContract(unittest.TestCase):
    def setup_entry(self):
        port, obs, ctx = Port(), page(), context()
        tools = FormEntryTools(port, application_id="fixture", require_write_guard=True)
        shown = tools.discover(obs, {}, context=ctx)
        return tools, port, obs, ctx, shown["entries"][0]["entry_id"]

    def receipt(self, before, *, ready="ready", **changes):
        values = dict(schema_version=1, command_id="c1", action="click", ok=True,
            args_echo={"ref": "e1", "epoch": before.epoch}, write_guard_held=True,
            observation=page(before.epoch + 1, ready))
        return ActionReceipt(**{**values, **changes})

    def test_private_native_reference_and_selector_do_not_enter_discovery(self):
        tools, _, obs, ctx, opaque = self.setup_entry()
        result = tools.discover(obs, {}, context=ctx)
        self.assertRegex(opaque, r"^form_entry_[0-9a-f]{32}$")
        self.assertNotIn("private-native-reference", json.dumps(result))
        self.assertNotIn('"ref"', json.dumps(result))

    def test_foreign_turn_actor_epoch_and_document_cannot_open_the_binding(self):
        for kind in ("actor", "turn", "epoch", "document"):
            tools, _, obs, ctx, opaque = self.setup_entry()
            if kind == "actor": ctx = context("reader")
            elif kind == "turn": ctx = context(turn="another")
            elif kind == "epoch": obs = page(2)
            else: obs = replace(obs, url="http://app.test/another")
            with self.subTest(kind=kind), self.assertRaises(BusinessToolRequestError):
                tools.plan(obs, {"entry_id": opaque}, context=ctx)

    def test_missing_or_forged_arguments_do_not_plan_an_entry(self):
        for args in ({}, {"entry_id": "private-native-reference"},
                     {"entry_id": "form_entry_" + "0" * 32, "ref": "e1"}):
            tools, _, obs, ctx, _ = self.setup_entry()
            with self.subTest(args=args), self.assertRaises(BusinessToolRequestError):
                tools.plan(obs, args, context=ctx)

    def test_only_a_guarded_fresh_matching_ack_can_bind_the_opened_form(self):
        for changes in ({"ok": False}, {"write_guard_held": False}, {"action": "observe"},
                        {"args_echo": {"ref": "another", "epoch": 1}},
                        {"observation": page(1)}, {"observation": None}):
            tools, port, obs, ctx, opaque = self.setup_entry()
            entry = tools.plan(obs, {"entry_id": opaque}, context=ctx)
            with self.subTest(changes=changes), self.assertRaises(BusinessToolRequestError):
                tools.acknowledged(entry, obs, self.receipt(obs, **changes), context=ctx)
            self.assertEqual(port.accepted, 0)
            self.assertEqual(port.released, 1)

    def test_pending_render_keeps_exact_ack_but_prevents_discovery_or_replay(self):
        tools, port, obs, ctx, opaque = self.setup_entry()
        entry = tools.plan(obs, {"entry_id": opaque}, context=ctx)
        receipt = self.receipt(obs, ready="pending")
        tools.acknowledged(entry, obs, receipt, context=ctx)
        with self.assertRaises(BusinessToolRequestError):
            tools.accept_current(receipt.observation, context=ctx)
        with self.assertRaises(BusinessToolRequestError):
            tools.discover(receipt.observation, {}, context=ctx)
        with self.assertRaises(BusinessToolRequestError):
            tools.plan(receipt.observation, {"entry_id": opaque}, context=ctx)
        self.assertEqual(tools.accept_current(page(3), context=ctx), "current-private-form")
        self.assertEqual((port.accepted, port.released), (1, 1))
        self.assertEqual(tools.accept_current(page(4), context=ctx), "")

    def test_ready_opener_receipt_still_needs_a_later_form_observation(self):
        tools, port, obs, ctx, opaque = self.setup_entry()
        entry = tools.plan(obs, {'entry_id': opaque}, context=ctx)
        receipt = self.receipt(obs)
        tools.acknowledged(entry, obs, receipt, context=ctx)
        with self.assertRaises(BusinessToolRequestError):
            tools.accept_current(receipt.observation, context=ctx)
        self.assertEqual((port.accepted, port.released), (0, 0))
        self.assertEqual(tools.accept_current(page(3), context=ctx), 'current-private-form')

    def test_identity_expiring_inside_the_application_retains_its_typed_error(self):
        tools, port, obs, ctx, _ = self.setup_entry()
        def expired(*args, **kwargs):
            raise PrincipalError('identity expired')
        port.form_entries = expired
        with self.assertRaises(BusinessToolIdentityError):
            tools.discover(obs, {}, context=ctx)

    def test_actor_change_clears_ack_instead_of_leaving_a_reusable_pending_ticket(self):
        tools, port, obs, ctx, opaque = self.setup_entry()
        entry = tools.plan(obs, {"entry_id": opaque}, context=ctx)
        tools.acknowledged(entry, obs, self.receipt(obs), context=ctx)
        with self.assertRaises(BusinessToolIdentityError):
            tools.accept_current(page(2), context=context("another"))
        self.assertIsNone(tools.pending)
        self.assertEqual(port.accepted, 0)
        self.assertEqual(port.released, 1)

    def test_cleanup_releases_pending_admission_without_opening_again(self):
        tools, port, obs, ctx, opaque = self.setup_entry()
        entry = tools.plan(obs, {"entry_id": opaque}, context=ctx)
        tools.acknowledged(entry, obs, self.receipt(obs, ready="pending"), context=ctx)
        tools.close()
        tools.close()
        self.assertEqual(port.released, 1)

    def test_expired_identity_while_pending_releases_the_acknowledged_admission(self):
        tools, port, obs, ctx, opaque = self.setup_entry()
        entry = tools.plan(obs, {"entry_id": opaque}, context=ctx)
        receipt = self.receipt(obs, ready="pending")
        tools.acknowledged(entry, obs, receipt, context=ctx)
        with patch('web_operation_agent.state.identity.time.time',
                                 return_value=ctx.expires_at / 1000 + 1):
            with self.assertRaises(BusinessToolIdentityError):
                tools.accept_current(receipt.observation, context=ctx)
        self.assertIsNone(tools.pending)
        self.assertEqual((port.accepted, port.released), (0, 1))

    def test_acknowledged_entry_cannot_be_reopened_after_verification_rejection(self):
        tools, port, obs, ctx, opaque = self.setup_entry()
        entry = tools.plan(obs, {"entry_id": opaque}, context=ctx)
        tools.acknowledged(entry, obs, self.receipt(obs), context=ctx)
        port.accept_entry = lambda *a, **kw: None
        with self.assertRaises(BusinessToolContractError):
            tools.accept_current(page(3), context=ctx)
        retried = tools.discover(page(2), {}, context=ctx)['entries'][0]['entry_id']
        with self.assertRaises(BusinessToolRequestError):
            tools.plan(page(2), {'entry_id': retried}, context=ctx)
        self.assertEqual(port.released, 1)

    def test_uncertain_dispatched_receipt_cannot_authorize_another_opening(self):
        tools, port, obs, ctx, opaque = self.setup_entry()
        entry = tools.plan(obs, {'entry_id': opaque}, context=ctx)
        with self.assertRaises(BusinessToolRequestError):
            tools.acknowledged(entry, obs, self.receipt(obs, ok=False), context=ctx)
        retry = tools.discover(page(2), {}, context=ctx)['entries'][0]['entry_id']
        with self.assertRaises(BusinessToolRequestError):
            tools.plan(page(2), {'entry_id': retry}, context=ctx)
        self.assertEqual(port.released, 1)

    def test_cleanup_failure_does_not_hide_an_expired_identity(self):
        tools, port, obs, ctx, opaque = self.setup_entry()
        entry = tools.plan(obs, {'entry_id': opaque}, context=ctx)
        tools.acknowledged(entry, obs, self.receipt(obs, ready='pending'), context=ctx)
        def broken(*args, **kwargs):
            raise ValueError('untrusted application cleanup')
        port.discard_entry = broken
        with patch('web_operation_agent.state.identity.time.time',
                   return_value=ctx.expires_at / 1000 + 1):
            with self.assertRaises(BusinessToolIdentityError) as caught:
                tools.accept_current(page(3), context=ctx)
        self.assertIsNone(tools.pending)
        self.assertNotIn('untrusted', str(caught.exception))
        self.assertIn('BusinessToolContractError', caught.exception.__notes__[0])

    def test_malformed_application_contract_is_typed_and_never_binds(self):
        for response in ([], (None,), (FormEntryCandidate('r', 'Prepare', 'create'),) * 2):
            tools, port, obs, ctx, _ = self.setup_entry()
            port.form_entries = lambda *a, **kw: response
            with self.subTest(response=response), self.assertRaises(BusinessToolContractError):
                tools.discover(obs, {}, context=ctx)
            self.assertFalse(tools.candidates)
        tools, port, obs, ctx, _ = self.setup_entry()
        def invalid(*a, **kw): raise ValueError('untrusted application response')
        port.form_entries = invalid
        with self.assertRaises(BusinessToolContractError):
            tools.discover(obs, {}, context=ctx)

    def test_missing_form_permission_refuses_discovery_before_application_read(self):
        tools, port, obs, ctx, _ = self.setup_entry()
        ctx = replace(ctx, principal=replace(ctx.principal, scopes=('records:read',)))
        port.form_entries = lambda *a, **kw: self.fail('ungranted form read was dispatched')
        with self.assertRaises(BusinessToolPermissionError):
            tools.discover(obs, {}, context=ctx)

    def test_applications_without_a_declared_guard_still_require_the_actual_receipt(self):
        port, obs, ctx = Port(), page(), context()
        tools = FormEntryTools(port, application_id='fixture')
        opaque = tools.discover(obs, {}, context=ctx)['entries'][0]['entry_id']
        entry = tools.plan(obs, {'entry_id': opaque}, context=ctx)
        tools.acknowledged(entry, obs, self.receipt(obs, write_guard_held=False), context=ctx)
        self.assertEqual(tools.accept_current(page(3), context=ctx), 'current-private-form')


if __name__ == "__main__":
    unittest.main()
