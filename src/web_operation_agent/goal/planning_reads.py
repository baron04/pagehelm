"""Narrow shared entity-read arguments to identities returned in this proposal.

This is a view of existing read contracts, not a new query or permission port.
Application handlers retain authority over catalog freshness and field semantics.
"""
from copy import deepcopy
import re

from ..business.entities import (DISCOVER_ENTITIES_TOOL, DESCRIBE_ENTITY_TOOL,
    QUERY_ENTITY_RECORDS_TOOL, RESOLVE_RECORD_REFERENCE_TOOL)


class PlanningReads:
    def __init__(self, schemas):
        self.original = tuple(schemas)
        self.entities = set()
        self.fields = {}
        # Partial/custom read surfaces keep their own contract. The shared entity
        # surface supplies the discovery needed to ground its opaque identifiers.
        self.grounded = {DISCOVER_ENTITIES_TOOL, DESCRIBE_ENTITY_TOOL, QUERY_ENTITY_RECORDS_TOOL}.issubset(
            {s['function']['name'] for s in schemas})

    def schemas(self):
        if not self.grounded:
            return self.original
        result = []
        for original in self.original:
            schema = deepcopy(original)
            function = schema['function']
            name, parameters = function['name'], function['parameters']
            properties = parameters.get('properties', {})
            if name == RESOLVE_RECORD_REFERENCE_TOOL:
                if self.entities:
                    properties['entity_id']['enum'] = sorted(self.entities)
                else:
                    properties.pop('entity_id', None)
                    parameters.pop('oneOf', None)
                    parameters['required'] = ['reference', 'entity_query']
            elif name == DESCRIBE_ENTITY_TOOL:
                if not self.entities:
                    continue
                properties['entity_id']['enum'] = sorted(self.entities)
            elif name == QUERY_ENTITY_RECORDS_TOOL:
                if not self.fields:
                    continue
                properties['entity_id']['enum'] = sorted(self.fields)
                branches = []
                for entity, fields in sorted(self.fields.items()):
                    bound = {'entity_id': {'enum': [entity]}}
                    for key in ('field_ids', 'aggregate_field_ids'):
                        bound[key] = {'items': {'enum': sorted(fields)}} if fields else {'maxItems': 0}
                    bound['filters'] = ({'items': {'properties': {
                        'field_id': {'enum': sorted(fields)}}}} if fields else {'maxItems': 0})
                    branches.append({'properties': bound})
                parameters['oneOf'] = branches
            result.append(schema)
        return tuple(result)

    def observe(self, name, content):
        if not self.grounded or not isinstance(content, dict):
            return
        schemas = {s['function']['name']: s['function']['parameters'] for s in self.original}
        # Patterns come from the shared published schema; no application vocabulary
        # or business text is parsed to manufacture an identity.
        descriptor = schemas.get(DESCRIBE_ENTITY_TOOL, {})
        entity_pattern = descriptor.get('properties', {}).get('entity_id', {}).get('pattern', '')
        query = schemas.get(QUERY_ENTITY_RECORDS_TOOL, {})
        field_pattern = query.get('properties', {}).get('field_ids', {}).get('items', {}).get('pattern', '')
        def entity(value):
            if isinstance(value, str) and entity_pattern and re.fullmatch(entity_pattern, value):
                self.entities.add(value)
                return value
            return None
        if name in (DISCOVER_ENTITIES_TOOL, RESOLVE_RECORD_REFERENCE_TOOL):
            for candidate in content.get('candidates', []):
                if isinstance(candidate, dict):
                    entity(candidate.get('entity_id'))
        elif name == DESCRIBE_ENTITY_TOOL:
            key = entity(content.get('entity_id'))
            if key is None:
                return
            fields = self.fields.setdefault(key, set())
            for field in content.get('fields', []):
                if isinstance(field, dict):
                    value = field.get('field_id')
                    if isinstance(value, str) and field_pattern and re.fullmatch(field_pattern, value):
                        fields.add(value)
                    entity(field.get('related_entity_id'))
