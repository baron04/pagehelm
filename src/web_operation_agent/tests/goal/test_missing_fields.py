"""Unavailable fields cannot cause unrelated-record queries or disappear in replies."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from web_operation_agent.goal.data import Choice, EntitySchema, FieldSchema, Ref, Row, Schema
from web_operation_agent.goal.executor import _not_done
from web_operation_agent.goal.resolve.indexing import index_from_schema
from web_operation_agent.goal.resolve.resolver import Resolver
from web_operation_agent.goal.spec import Deliverable, GoalSpec


class MissingFieldTests(unittest.TestCase):
    def resolve(self, fields, *, via=""):
        schema = Schema(entities=(
            EntitySchema("order", "Order", fields=(FieldSchema("customer", "Customer", "link", relation="contact"),)),
            EntitySchema("contact", "Contact", fields=(FieldSchema("name", "Name", "text"),))), pages=())
        port=SimpleNamespace(find=Mock(side_effect=AssertionError("unrelated lookup")),
                             rows=Mock(side_effect=AssertionError("unrelated candidates")))
        resolver=Resolver(port,index_from_schema(schema),schema)
        result=resolver.resolve(GoalSpec(deliverables=(Deliverable(
            kind="fact",entity="order" if via else "contact",via=via,fields=fields),)))
        port.find.assert_not_called();port.rows.assert_not_called()
        return result.deliverables[0]

    def test_unknown_fields_refuse_before_record_disambiguation(self):
        result=self.resolve(("Phone", "API key"))
        self.assertEqual(result.missing_fields,("Phone","API key"))
        self.assertFalse(result.candidates)
        self.assertIn("Phone",_not_done(result,False))
        self.assertIn("API key",_not_done(result,True))

    def test_linked_target_schema_is_used_without_guessing_its_record(self):
        result=self.resolve(("Phone",),via="Customer")
        self.assertEqual(result.missing_fields,("Phone",))
        self.assertFalse(result.candidates)

    def test_owner_link_cannot_silently_redirect_fields_of_the_named_entity(self):
        schema = Schema(entities=(
            EntitySchema("ticket", "Ticket", fields=(
                FieldSchema("owner", "Assigned team", "link", relation="team"),
                FieldSchema("priority", "Priority", "text"))),
            EntitySchema("team", "Team", fields=(FieldSchema("name", "Name", "text"),))), pages=())
        port = SimpleNamespace(find=Mock(return_value=(Ref("ticket", "1", "T-1"),)),
            read=Mock(return_value=Row("1", "T-1", {"priority": "High"}, {"priority": "High"})),
            name_of=lambda text: text)
        resolver = Resolver(port, index_from_schema(schema), schema)
        bad = Deliverable(kind="fact", entity="ticket", record="T-1",
                          via="Assigned team", fields=("Priority",))
        result = resolver.resolve(GoalSpec(deliverables=(bad,))).deliverables[0]
        self.assertFalse(result.facts)
        self.assertIn("omit via", result.repair)
        self.assertIn("Assigned team", result.repair)
        self.assertFalse(result.missing_fields)
        port.find.assert_not_called(); port.read.assert_not_called()
        # Only an explicit corrected declaration can read the original record.
        from dataclasses import replace
        result = resolver.resolve(GoalSpec(deliverables=(replace(bad, via=""),))).deliverables[0]
        self.assertEqual(result.facts[0].value, "High")
        port.read.assert_called_once()

    def test_only_observed_declared_choice_codes_are_forwarded(self):
        schema=Schema(entities=(EntitySchema("record","Record",fields=(
            FieldSchema("state","Status","choice",choices=(Choice("0","Draft"),Choice("1","Review"))),
            FieldSchema("note","Note","text")),),),pages=())
        for raw, expected in ((0,"0"),(1,"1"),("1","1"),(True,""),("unknown",""),(None,"")):
            with self.subTest(raw=raw):
                port=SimpleNamespace(read=lambda *a,**kw: Row("1","R",{"state":raw,"note":"text"},
                                                             {"state":"Shown","note":"text"}))
                resolver=Resolver(port,index_from_schema(schema),schema)
                facts=resolver._facts("record","1","R",("Status","Note")).facts
                self.assertEqual(facts[0].native_value,expected)
                self.assertEqual(facts[0].field_key,"state")
                self.assertEqual(facts[1].native_value,"")
