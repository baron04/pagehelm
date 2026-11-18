"""Editor effect checks preserve the existing one-use FormPort handoff."""
from dataclasses import replace
import unittest

from web_operation_agent.business.errors import (
    BusinessToolIdentityError, BusinessToolPermissionError, BusinessToolRequestError)
from web_operation_agent.execution.action_admission import (
    ActionIntent, ActionAdmissionRefused, ActionEffect, ActionResultPending)
from web_operation_agent_yudao.writes.form_open_actions import NativeFormOpenActions
from web_operation_agent_yudao.writes.form_entries import NativeEntryFormPort, NativeEntryVerifiers, REVIEWED_FILES
from . import test_form_entries as entries


class NativeFormOpenTests(unittest.TestCase):
    setUp = entries.EntryTests.setUp
    creation = entries.CreationEntryTests.creation

    def admission(self, verifier=None, before=None, reference='customer-update:12'):
        verifier, before = verifier or self.verifier, before or self.before
        port = NativeEntryFormPort(NativeEntryVerifiers(verifier))
        entry = port.plan_entry(reference,before,context=self.context)
        actions = NativeFormOpenActions(port)
        intent = ActionIntent.create('click',{'ref':entry.control.ref},before,self.context)
        permit = actions.admit(intent,context=self.context,checkpoint=lambda:None)
        return actions,port,entry,intent,permit

    def test_navigation_verification_preserves_form_binding_for_one_acceptance(self):
        actions,port,entry,intent,permit=self.admission()
        self.assertEqual(permit.effect,ActionEffect.NAVIGATION)
        self.assertTrue(actions.verify(permit,intent,self.receipt,observation=self.after,
                                      context=self.context,checkpoint=lambda:None))
        self.assertIsNone(self.verifier._current)
        self.assertIsNotNone(self.verifier._planned)
        self.assertTrue(port.accept_entry(entry,self.before,self.receipt,self.after,context=self.context))
        self.assertIsNone(self.verifier._planned)
        self.assertFalse(actions.verify(permit,intent,self.receipt,observation=self.after,
                                       context=self.context,checkpoint=lambda:None))

    def test_creation_uses_its_own_review_and_consumes_only_on_form_acceptance(self):
        _,_,verifier,before,after=self.creation()
        actions,port,entry,intent,permit=self.admission(verifier,before,'customer-create')
        self.assertTrue(actions.verify(permit,intent,self.receipt,observation=after,
                                      context=self.context,checkpoint=lambda:None))
        self.assertTrue(port.accept_entry(entry,before,self.receipt,after,context=self.context))

    def test_reviewed_creation_query_location_is_preserved_and_cannot_switch_tabs(self):
        _,_,verifier,before,after=self.creation('project-create')
        before=replace(before,url=before.url+'?tabs=participate')
        after=replace(after,url=before.url)
        actions,port,entry,intent,permit=self.admission(verifier,before,'project-create')
        self.assertTrue(actions.verify(permit,intent,self.receipt,observation=after,
                                      context=self.context,checkpoint=lambda:None))
        self.assertFalse(actions.verify(permit,intent,self.receipt,
            observation=replace(after,url=after.url.replace('participate','owner')),
            context=self.context,checkpoint=lambda:None))

    def test_raw_click_fill_wrong_actor_and_wrong_turn_do_not_gain_entry_authority(self):
        actions=NativeFormOpenActions(self.port)
        intent=ActionIntent.create('click',{'ref':'e1'},self.before,self.context)
        self.assertFalse(actions.matches(intent))
        with self.assertRaises(ActionAdmissionRefused):
            actions.admit(intent,context=self.context,checkpoint=lambda:None)
        self.port.plan_entry('customer-update:12',self.before,context=self.context)
        self.assertFalse(actions.matches(ActionIntent.create('fill',{'ref':'e1','value':'Draft'},self.before,self.context)))
        for context in (replace(self.context,turn_id='other'),
                        replace(self.context,principal=replace(self.context.principal,actor_id='3'))):
            with self.subTest(context=context.turn_id),self.assertRaises(ActionAdmissionRefused):
                actions.admit(intent,context=context,checkpoint=lambda:None)

    def test_mounting_waits_on_original_receipt_and_mismatched_dialog_refuses(self):
        actions,_,_,intent,permit=self.admission()
        with self.assertRaises(ActionResultPending):
            actions.verify(permit,intent,self.receipt,observation=replace(self.before,epoch=2,elements=[]),
                           context=self.context,checkpoint=lambda:None)
        for after in (replace(self.after,record_route='/crm/customer/detail/13'),
                      replace(self.after,elements=[replace(e,attrs={**e.attrs,'form_instance':''}) for e in self.after.elements])):
            self.assertFalse(actions.verify(permit,intent,self.receipt,observation=after,
                                            context=self.context,checkpoint=lambda:None))

    def test_authority_and_review_are_rechecked_after_acknowledgement(self):
        actions,_,_,intent,permit=self.admission()
        self.client.level=2
        with self.assertRaises(BusinessToolPermissionError):
            actions.verify(permit,intent,self.receipt,observation=self.after,context=self.context,checkpoint=lambda:None)
        self.client.level=3;self.client.actor=3
        with self.assertRaises(BusinessToolIdentityError):
            actions.verify(permit,intent,self.receipt,observation=self.after,context=self.context,checkpoint=lambda:None)
        self.client.actor=2
        self.tree.resolve(REVIEWED_FILES[2]).write_text('<template>replaced</template>')
        with self.assertRaises(BusinessToolRequestError):
            actions.verify(permit,intent,self.receipt,observation=self.after,context=self.context,checkpoint=lambda:None)

    def test_failed_unguarded_or_other_receipt_never_verifies(self):
        actions,_,_,intent,permit=self.admission()
        for changes in ({'ok':False},{'write_guard_held':False},{'action':'observe'},
                        {'args_echo':{'ref':'other','epoch':1}},{'navigated':True}):
            with self.subTest(changes=changes):
                self.assertFalse(actions.verify(permit,intent,replace(self.receipt,**changes),
                    observation=self.after,context=self.context,checkpoint=lambda:None))
