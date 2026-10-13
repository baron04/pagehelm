"""Current read identities constrain the Skill surface, never its authority."""
import unittest
from jsonschema import Draft202012Validator, ValidationError

from web_operation_agent.business.entities import ENTITY_READ_SCHEMAS, RESOLVE_RECORD_REFERENCE_SCHEMA
from web_operation_agent.goal.planning_reads import PlanningReads


class PlanningReadTests(unittest.TestCase):
    def setUp(self):
        self.surface = PlanningReads((*ENTITY_READ_SCHEMAS, RESOLVE_RECORD_REFERENCE_SCHEMA))
        self.entity = 'entity_' + 'a' * 32
        self.field = 'field_' + 'b' * 32

    def schema(self, name):
        return next(s['function']['parameters'] for s in self.surface.schemas() if s['function']['name'] == name)

    def test_unobserved_entity_ids_are_not_an_option_and_queries_require_description(self):
        names = {s['function']['name'] for s in self.surface.schemas()}
        self.assertNotIn('describe_entity', names)
        self.assertNotIn('query_entity_records', names)
        schema = self.schema('resolve_record_reference')
        Draft202012Validator(schema).validate({'entity_query': 'Contacts', 'reference': 'Synthetic'})
        with self.assertRaises(ValidationError):
            Draft202012Validator(schema).validate({'entity_id': self.entity, 'reference': 'Synthetic'})

    def test_field_identity_must_have_been_described_for_the_selected_entity(self):
        self.surface.observe('discover_entities', {'candidates': [{'entity_id': self.entity}]})
        Draft202012Validator(self.schema('describe_entity')).validate({'entity_id': self.entity})
        self.surface.observe('describe_entity', {'entity_id': self.entity, 'fields': [{'field_id': self.field}]})
        other = 'entity_' + 'c' * 32
        other_field = 'field_' + 'd' * 32
        self.surface.observe('describe_entity', {'entity_id': other, 'fields': [{'field_id': other_field}]})
        validator = Draft202012Validator(self.schema('query_entity_records'))
        validator.validate({'entity_id': self.entity, 'field_ids': [self.field]})
        validator.validate({'entity_id': self.entity, 'field_ids': [], 'filters': [
            {'field_id': self.field, 'operator': 'contains', 'value': 'Synthetic'}]})
        for args in (
            {'entity_id': self.entity, 'field_ids': [other_field]},
            {'entity_id': self.entity, 'field_ids': [], 'aggregate_field_ids': [other_field]},
            {'entity_id': self.entity, 'field_ids': [], 'filters': [
                {'field_id': other_field, 'operator': 'contains', 'value': 'Synthetic'}]},
        ):
            with self.subTest(args=args), self.assertRaises(ValidationError):
                validator.validate(args)

    def test_empty_description_allows_count_but_no_unobserved_fields(self):
        self.surface.observe('describe_entity', {'entity_id': self.entity, 'fields': []})
        validator = Draft202012Validator(self.schema('query_entity_records'))
        validator.validate({'entity_id': self.entity, 'field_ids': []})
        with self.assertRaises(ValidationError):
            validator.validate({'entity_id': self.entity, 'field_ids': [self.field]})

    def test_a_query_record_body_cannot_issue_a_new_catalog_identity(self):
        self.surface.observe('query_entity_records', {'entity_id': self.entity, 'fields': [{'field_id': self.field}]})
        self.assertEqual(self.surface.entities, set())
        self.assertEqual(self.surface.fields, {})

    def test_custom_reads_and_published_schemas_remain_independent(self):
        custom = {'type': 'function', 'function': {'name': 'list_contacts', 'parameters': {'type': 'object'}}}
        surface = PlanningReads((custom,))
        self.assertEqual(surface.schemas(), (custom,))
        self.assertNotIn('enum', RESOLVE_RECORD_REFERENCE_SCHEMA['function']['parameters']['properties']['entity_id'])
