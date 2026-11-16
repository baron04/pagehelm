"""Parallel reference reads retain order, bounds and refusals without sharing resolvers."""
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from web_operation_agent.goal.data import DataRefused, Ref
from web_operation_agent.goal.index import ApplicationIndex, IndexEntity, IndexPage
from web_operation_agent.goal.resolve.resolver import Ambiguous, Found
from web_operation_agent.goal.resolve.service import DataGoalPort


class ReferenceConcurrencyTests(unittest.TestCase):
    def test_parallel_reads_are_bounded_and_results_keep_index_order(self):
        keys = ('slow', 'fast', 'refused', 'ambiguous', 'last', 'extra')
        index = ApplicationIndex(entities=tuple(IndexEntity(k, k) for k in keys),
                                 pages=tuple(IndexPage('/'+k, k, k, k) for k in keys))
        data = SimpleNamespace(index=lambda **kw: index)
        port = DataGoalPort(data, reference_workers=4)
        lock = threading.Lock()
        barrier = threading.Barrier(4)
        active = 0
        peak = 0
        readers = []
        visited = []

        class Reader:
            def view(self, entity):
                return object()

            def _find_record(self, entity, text):
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                    visited.append(entity)
                    first_batch = len(visited) <= 4
                try:
                    if first_batch:
                        barrier.wait(timeout=2)
                    time.sleep(.03 if entity == 'slow' else .002)
                    if entity == 'refused':
                        raise DataRefused('parent required')
                    if entity == 'ambiguous':
                        return Ambiguous(('ABC123 one', 'ABC123 two'))
                    return Found(Ref(entity, entity, 'ABC123 ' + entity))
                finally:
                    with lock:
                        active -= 1

            def _own_name(self, display):
                return display

        def reader(_index):
            instance = Reader()
            readers.append(instance)
            return instance

        with patch.object(port, '_resolver', side_effect=reader):
            found = port.locate_reference('ABC123')
        self.assertEqual(found, (('slow', 'ABC123 slow'), ('fast', 'ABC123 fast'),
                                 ('last', 'ABC123 last')))
        self.assertEqual(peak, 4)
        self.assertCountEqual(visited, keys)
        self.assertEqual(len({id(r) for r in readers}), 6)

    def test_a_lookup_may_be_held_to_some_of_the_entities_a_page_shows(self):
        keys = ('a', 'b', 'c')
        index = ApplicationIndex(entities=tuple(IndexEntity(k, k) for k in keys + ('unpaged',)),
                                 pages=tuple(IndexPage('/'+k, k, k, k) for k in keys))
        port, visited = DataGoalPort(SimpleNamespace(index=lambda **kw: index)), []

        class Reader:
            def view(self, entity):
                return object()

            def _find_record(self, entity, text):
                visited.append(entity)
                return Found(Ref(entity, entity, 'ABC123 ' + entity))

            def _own_name(self, display):
                return display

        with patch.object(port, '_resolver', side_effect=lambda _index: Reader()):
            found = port.locate_reference('ABC123', entities=('c', 'unpaged', 'a'))
        # Index order, and only entities a page shows.
        self.assertEqual((found, visited), ((('a', 'ABC123 a'), ('c', 'ABC123 c')), ['a', 'c']))

    def test_records_named_tells_the_one_record_or_the_several_with_their_addresses(self):
        keys = ('one', 'two', 'none', 'refused', 'other')
        index = ApplicationIndex(entities=tuple(IndexEntity(k, k) for k in keys),
                                 pages=tuple(IndexPage('/'+k, k, k, k) for k in keys))

        def address(entity, rid):
            if entity == 'two' and rid == '2':
                raise DataRefused('no record form')
            return f'/{entity}/detail/{rid}'
        port = DataGoalPort(SimpleNamespace(index=lambda **kw: index, record_address=address))
        twins = (Ref('two', '1', '李文博（ID=1）'), Ref('two', '2', '李文博（ID=2）'), Ref('two', '3', '李文博士'))

        class Reader:
            def view(self, entity):
                return object()

            def _find_record(self, entity, text):
                if entity == 'one':
                    return Found(Ref('one', '7', '李文博'))
                if entity == 'two':
                    return Ambiguous(('李文博（ID=1）', '李文博（ID=2）'))
                if entity == 'refused':
                    raise DataRefused('parent required')
                if entity == 'other':
                    return Found(Ref('other', '9', '王五'))      # found, but not by this name
                return None

            def _find(self, entity, text, limit):
                return twins

            def _own_name(self, display):
                return display

        with patch.object(port, '_resolver', side_effect=lambda _index: Reader()):
            found = port.records_named('李文博')
            held = port.records_named('李文博', entities=('two',))
            diagnosed, failures = port.records_named_diagnostics('李文博')
        self.assertEqual(found, (('one', (('7', '李文博', '/one/detail/7'),)),
                                 ('two', (('1', '李文博（ID=1）', '/two/detail/1'), ('2', '李文博（ID=2）', '')))))
        self.assertEqual(held, (found[1],))
        self.assertEqual(diagnosed, found)
        self.assertEqual(failures, (('refused', False, False),))
        self.assertEqual(port.records_named('2026-10-01'), ())

    def test_records_named_accepts_shorthand_substrings_and_honors_searchable_scope(self):
        keys = ('customer', 'coupon', 'unindexed')
        index = ApplicationIndex(entities=tuple(IndexEntity(k, k) for k in keys),
                                 pages=tuple(IndexPage('/' + k, k, k, k) for k in keys))
        visited = []
        port = DataGoalPort(SimpleNamespace(
            index=lambda **kw: index,
            record_address=lambda entity, rid: f'/{entity}/{rid}',
            name_searchable=lambda entity, _text: entity != 'unindexed',
        ))

        class Reader:
            def view(self, entity):
                return object()

            def _find_record(self, entity, text):
                visited.append(entity)
                if entity == 'customer':
                    return Found(Ref('customer', '12', '深圳远辰启源工程技术有限责任公司'))
                if entity == 'coupon':
                    return Found(Ref('coupon', '5', '青屿满99减15新人专享券'))
                if entity == 'unindexed':
                    return Found(Ref('unindexed', '99', '深圳远辰日志'))
                return None

            def _own_name(self, display):
                return display

        with patch.object(port, '_resolver', side_effect=lambda _index: Reader()):
            found = port.records_named('远辰')
            # Prefix-strict locate_reference stays conservative for implicit goal declarations.
            implicit = port.locate_reference('远辰')
            visited.clear()
            explicit = port.records_named('远辰', entities=('unindexed',))
        self.assertEqual(found, (('customer', (('12', '深圳远辰启源工程技术有限责任公司', '/customer/12'),)),))
        self.assertNotIn('unindexed', ['customer', 'coupon'])
        self.assertEqual(implicit, ())
        self.assertEqual(explicit, (('unindexed', (('99', '深圳远辰日志', '/unindexed/99'),)),))
        self.assertEqual(visited, ['unindexed'])

    def test_worker_configuration_is_bounded_and_default_is_sequential(self):
        data = object()
        self.assertEqual(DataGoalPort(data).reference_workers, 1)
        for workers in (0, 9, True, 1.5):
            with self.assertRaises(ValueError):
                DataGoalPort(data, reference_workers=workers)
