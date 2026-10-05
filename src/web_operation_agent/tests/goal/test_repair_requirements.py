"""Repairs retain targets across generic entity and page declaration structures."""
import unittest

from web_operation_agent.goal.repair import field_values_preserved, lost_requirements
from web_operation_agent.goal.spec import Deliverable, GoalSpec
from web_operation_agent.goal.index import ApplicationIndex, IndexEntity, IndexField
from dataclasses import replace


def spec(**value):
    return GoalSpec.from_json({"deliverables": [{"kind": "page", "entity": "contacts", **value}]})


class RepairRequirementsTests(unittest.TestCase):
    def test_form_label_repair_uses_unique_actor_fields_not_strings(self):
        for label, alias in (("邮箱", "邮箱地址"), ("Email", "Electronic mail")):
            with self.subTest(label=label):
                index = ApplicationIndex(entities=(IndexEntity("people", "People", fields=(
                    IndexField("email", label, "text", aliases=(alias,)),
                    IndexField("name", "Name", "text"))),), pages=())
                old = Deliverable("handoff", entity="wrong", record="Acme",
                                  field_values=((alias, "a@x.invalid"), ("Name", " A  B ")))
                new = replace(old, entity="people", field_values=(("Name", " A  B "), (label, "a@x.invalid")))
                self.assertTrue(field_values_preserved(old, new, index))
                for bad in (replace(new, record="Other"), replace(new, action="create"),
                            replace(new, via="Owner"), replace(new, field_values=((label, "b@x.invalid"),)),
                            replace(new, field_values=((label, "a@x.invalid"), ("Name", "A B"))),
                            replace(new, field_values=((label, " A  B "), ("Name", "a@x.invalid")))):
                    self.assertFalse(field_values_preserved(old, bad, index))

    def test_field_repair_refuses_ambiguous_aliases_duplicate_bindings_and_substrings(self):
        old = Deliverable("handoff", entity="people", record="Acme",
                          field_values=(("Electronic mail", "a@x.invalid"),))
        new = replace(old, field_values=(("Email", "a@x.invalid"),))
        def index(*fields):
            return ApplicationIndex(entities=(IndexEntity("people", "People", fields=fields),), pages=())
        field = IndexField("email", "Email", "text", aliases=("Electronic mail",))
        self.assertFalse(field_values_preserved(old, new, None))
        self.assertFalse(field_values_preserved(old, new, index(
            field, IndexField("other", "Other", "text", aliases=("Electronic mail",)))))
        self.assertFalse(field_values_preserved(replace(old, field_values=(("mail", "a@x.invalid"),)),
                                               new, index(field)))
        duplicate = replace(old, field_values=(("Email", "a"), ("Electronic mail", "a")))
        self.assertFalse(field_values_preserved(duplicate, replace(duplicate, field_values=(
            ("email", "a"), ("Email", "a"))), index(field)))

    def test_a_record_description_can_be_corrected_to_selection_conditions(self):
        before = spec(record="the quotation sent to Acme", section="Items")
        after = spec(section="Items", metric={"op": "count", "filters": [
            {"field": "Customer", "values": ["Acme"]}]})
        self.assertEqual(lost_requirements(before, after), ())

    def test_wrong_entity_label_requires_an_exact_literal_user_assignment(self):
        index = ApplicationIndex(entities=(IndexEntity('crm', '客户', fields=(
            IndexField('email', '邮箱', 'text'), IndexField('phone', '电话', 'text'))),), pages=())
        old = Deliverable('handoff',entity='oa',record='晨岚机电',field_values=(('邮箱地址','a@x.invalid'),))
        new = replace(old,entity='crm',field_values=(('邮箱','a@x.invalid'),))
        self.assertTrue(field_values_preserved(old,new,index,'把晨岚机电的邮箱改填a@x.invalid，填好后留给我点确定。'))
        for request in ('把晨岚机电的电话改填a@x.invalid。', '不要把晨岚机电的邮箱改填a@x.invalid。',
                        '把晨岚机电的备用邮箱改填a@x.invalid。', '把晨岚机电的邮箱改填A@x.invalid。',
                        '把晨岚机电的邮箱改填a@x.invalid.extra。',
                        '邮箱改填a@x.invalid，邮箱改填a@x.invalid。'):
            self.assertFalse(field_values_preserved(old,new,index,request),request)
        self.assertFalse(field_values_preserved(old,replace(new,record='别人'),index,'邮箱填a@x.invalid'))
        self.assertFalse(field_values_preserved(old,replace(new,field_values=(('电话','a@x.invalid'),)),index,'邮箱填a@x.invalid'))
        english = ApplicationIndex(entities=(IndexEntity('crm', 'People', fields=(
            IndexField('email', 'Email', 'text'),)),), pages=())
        corrected = replace(new, field_values=(('Email','a@x.invalid'),))
        self.assertTrue(field_values_preserved(old,corrected,english,'Set Email to a@x.invalid; stop.'))
        self.assertFalse(field_values_preserved(old,corrected,english,'Secondary Email to a@x.invalid; stop.'))

    def test_corrected_names_and_labels_keep_the_same_typed_requirements(self):
        before = spec(record="Acme customer", section="Contacts", page={"key": "wrong", "view": "list"})
        after = spec(record="Acme", section="Contact people", page={"key": "people", "view": "list"})
        self.assertEqual(lost_requirements(before, after), ())

    def test_partial_query_and_exclusion_cannot_be_removed(self):
        before = spec(kind="list", metric={"op": "count", "group_by": "Region", "filters": [
            {"field": "Owner", "values": ["Acme"]},
            {"field": "Status", "op": "!=", "values": ["Archived"]}]})
        for conditions in ([{"field": "Owner", "values": ["Acme"]}],
                           [{"field": "Owner", "values": ["Acme"]},
                            {"field": "Status", "op": "=", "values": ["Archived"]}]):
            after = spec(kind="list", metric={"op": "count", "filters": conditions})
            self.assertIn("deliverable 1: query exclusion removed", lost_requirements(before, after))
            self.assertIn("deliverable 1: query grouping removed", lost_requirements(before, after))

    def test_correcting_a_query_label_does_not_lose_the_query(self):
        before = spec(kind="list", metric={"op": "count", "filters": [
            {"field": "Validity", "values": ["2026-09"]}]})
        after = spec(kind="list", metric={"op": "count", "filters": [
            {"field": "Expiration", "values": ["2026-09"]}]})
        self.assertEqual(lost_requirements(before, after), ())
