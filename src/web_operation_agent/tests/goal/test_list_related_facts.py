"""Related facts bind exact actor-read list IDs, including every numeric tie."""
import unittest
from dataclasses import replace

from web_operation_agent.goal.data import DataRefused, EntitySchema, FieldSchema, LinkValue, Row, Schema, matches
from web_operation_agent.goal.resolve.indexing import index_from_schema
from web_operation_agent.goal.resolve.resolver import Resolver
from web_operation_agent.goal.spec import Deliverable, GoalSpec, Metric


class Port:
    def __init__(self):
        self.schema = Schema(entities=(
            EntitySchema('task', 'Tasks', fields=(
                FieldSchema('score', 'Score', 'number', listed=True),
                FieldSchema('owner', 'Owner', 'link', relation='person', listed=True, aliases=('负责人',)))),
            EntitySchema('person', 'People', fields=(FieldSchema('email', 'Email', 'text'),))), pages=())
        self.tasks = tuple(Row(str(i), f'Task {i}', {'score': n, 'owner': LinkValue(f'p{i}', 'Same display')},
                              {'score': str(n), 'owner': 'Same display'}) for i, n in enumerate((9, 9, 3)))
        self.reads = []
        self.refuse = False
        self.truncate = False

    def name_of(self, entity): return entity
    def time_zone(self): return 'UTC'
    def count(self, entity, where):
        if entity == 'task': return sum(matches(where, row) for row in self.tasks)
        return 2
    def rows(self, entity, where, *, fields=(), order=(), limit=80):
        if entity != 'task':
            return (Row('p0', 'Same display'), Row('p1', 'Same display'))[:limit]
        rows = [replace(row, values={f:row.values[f] for f in fields},
                        shown={f:row.shown[f] for f in fields}) for row in self.tasks if matches(where, row)]
        return tuple(rows[:1] if self.truncate else rows[:limit])
    def read(self, entity, rid, fields):
        self.reads.append((entity, rid, tuple(fields)))
        if self.refuse: raise DataRefused('actor denies linked record', permission_denied=True)
        return Row(rid, 'Same display', {'email': f'{rid}@x.invalid'}, {'email': f'{rid}@x.invalid'})


class ListRelatedFactsTests(unittest.TestCase):
    def setUp(self):
        self.port = Port()
        self.resolver = Resolver(self.port, index_from_schema(self.port.schema), self.port.schema)
        self.list = Deliverable('list', entity='task', fields=('Score','Owner'), metric=Metric('max','Score'))
        self.fact = Deliverable('fact', entity='person', via='Owner', fields=('Email',))

    def resolve(self, listed=None, fact=None):
        return self.resolver.resolve(GoalSpec((listed or self.list, fact or self.fact)))

    def test_every_tied_record_keeps_its_link_and_reads_by_id_despite_duplicate_names(self):
        result = self.resolve()
        self.assertEqual(result.deliverables[0].aggregate.count, 3)
        self.assertEqual([f.value for f in result.deliverables[1].facts], ['p0@x.invalid','p1@x.invalid'])
        self.assertEqual(self.port.reads, [('person','p0',('email',)),('person','p1',('email',))])
        self.assertTrue(all('Task 2' not in f.subject for f in result.deliverables[1].facts))

    def test_complete_ordinary_list_follows_only_its_returned_links(self):
        result = self.resolve(replace(self.list, metric=Metric('count')))
        self.assertEqual(len(result.deliverables[1].facts),3)

    def test_unread_link_column_never_becomes_a_global_customer_query(self):
        result = self.resolve(replace(self.list, fields=('Score',)))
        self.assertIn('unread',result.deliverables[1].problem)
        self.assertEqual(self.port.reads,[])

    def test_incomplete_numeric_read_does_not_bind_an_invented_winner(self):
        self.port.truncate=True
        result=self.resolve()
        self.assertTrue(result.deliverables[0].problem)
        self.assertTrue(result.deliverables[1].problem)
        self.assertEqual(self.port.reads,[])

    def test_truncated_ordinary_list_cannot_fall_back_to_a_global_target(self):
        self.port.truncate = True
        result = self.resolve(replace(self.list, metric=Metric('count')))
        self.assertIn('complete', result.deliverables[1].problem)
        self.assertEqual(self.port.reads, [])

    def test_unshown_rows_of_a_limited_list_are_not_followed(self):
        result = self.resolve(replace(self.list, metric=Metric('count'), limit=2))
        self.assertFalse(result.deliverables[0].aggregate.complete)
        self.assertIn('complete', result.deliverables[1].problem)
        self.assertEqual(self.port.reads, [])

    def test_empty_complete_list_returns_zero_without_reading_any_target(self):
        self.port.tasks = ()
        result = self.resolve(replace(self.list, metric=Metric('count')))
        self.assertEqual(result.deliverables[1].aggregate.count, 0)
        self.assertEqual(self.port.reads, [])

    def test_link_labels_match_exact_names_and_aliases(self):
        self.assertTrue(self.resolve(fact=replace(self.fact,via='Own')).deliverables[1].problem)
        self.assertEqual(self.port.reads,[])
        self.assertEqual(len(self.resolve(fact=replace(self.fact,via='负责人')).deliverables[1].facts),2)

    def test_named_filtered_rule_and_navigation_deliverables_are_not_rebound(self):
        for change in ({'record':'Specific'}, {'metric':Metric('count')}, {'rules':('r1',)}, {'kind':'page'}):
            with self.subTest(change=change):
                self.assertIsNone(self.resolver._linked_list_facts(replace(self.fact,**change),('task',self.port.tasks)))

    def test_permission_denial_does_not_return_partial_related_facts(self):
        self.port.refuse=True
        result=self.resolve().deliverables[1]
        self.assertEqual(result.denied,'read')
        self.assertEqual(result.facts,())

    def test_list_context_does_not_survive_a_separate_resolution(self):
        self.resolve()
        self.port.reads.clear()
        result=self.resolver.resolve(GoalSpec((self.fact,))).deliverables[0]
        self.assertTrue(result.problem)
        self.assertEqual(self.port.reads,[])


if __name__=='__main__': unittest.main()
