"""Unknown form values never authorize clearing or an approval dispatch."""
import unittest
from web_operation_agent.goal.coverage import missing_form_values
from web_operation_agent.goal.index import ApplicationIndex, IndexEntity, IndexField
from web_operation_agent.goal.spec import Deliverable

INDEX = ApplicationIndex(entities=(IndexEntity('people', 'People', fields=(
    IndexField('email', 'Email', 'text', aliases=('邮箱',)),
    IndexField('phone', 'Phone', 'text', aliases=('电话',)),
    IndexField('secondary', 'Secondary Email', 'text', aliases=('备用邮箱',)))),), pages=(), source_digest='synthetic')

class Values(unittest.TestCase):
    def test_missing_and_negated_values_need_input(self):
        d = Deliverable('handoff',entity='people',field_values=(('Email',''),))
        for text in ('给Ada补个邮箱', 'Set Ada’s Email', '不要把邮箱清空', 'Do not clear Email',
                     '电话清空，补个邮箱', '备用邮箱清空', '备注改成“清空邮箱”'):
            with self.subTest(text=text):
                result=missing_form_values(d,text,INDEX)
                self.assertEqual(result.input_fields,('Email',))
                self.assertIsNone(result.page)
                self.assertFalse(result.repair)

    def test_explicit_lists_and_real_values_keep_the_contract(self):
        d=Deliverable('handoff',entity='people',field_values=(('Email',''),('Phone','')))
        for text in ('把邮箱和电话清空', '清空邮箱、电话', 'Clear Email and Phone', 'Email、Phone置空'):
            self.assertIsNone(missing_form_values(d,text,INDEX),text)
        self.assertIsNone(missing_form_values(Deliverable('handoff',entity='people',
            field_values=(('Email','new@example.invalid'),)),'Set Email to new@example.invalid',INDEX))

    def test_scope_offers_only_requested_record_subpages(self):
        from web_operation_agent.goal.narrow import summaries
        from dataclasses import replace
        index=replace(INDEX,entities=(replace(INDEX.entities[0],sections=("Terms", "Contacts", "合同")),))
        self.assertIn("record subpages: 合同", summaries(index,"打开Ada的合同页",()))
        self.assertNotIn("record subpages:", summaries(index,"Ada的Email是什么",()))
        self.assertIn("record subpages: Terms", summaries(index,"Open Ada’s Terms page",()))

    def test_typed_missing_field_and_nonform_control(self):
        r=missing_form_values(Deliverable('handoff',entity='people',fields=('邮箱',)), '补邮箱', INDEX)
        self.assertEqual(r.input_fields,('Email',))
        self.assertIsNone(missing_form_values(Deliverable('handoff',entity='people',action='Approve'),'Approve Ada',INDEX))
        self.assertIsNone(missing_form_values(Deliverable('fact',entity='people',fields=('Email',)),'Email?',INDEX))

if __name__=='__main__':unittest.main()
