"""Offline trust-boundary checks for the synthetic-data operator tools."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

DEPLOY = Path(__file__).resolve().parents[3] / 'deploy'
sys.path.insert(0, str(DEPLOY))
import demo_data_common as common
import demo_data


class DemoDataTests(unittest.TestCase):
    def test_scope_preserves_core_and_excludes_write_buttons_and_hidden_descendants(self):
        def menu(i, parent, path='', component='', permission='', kind=2, name='menu'):
            return dict(id=i, parentId=parent, path=path, component=component,
                        permission=permission, type=kind, name=name, status=0)
        scope = common.menu_scope([
            menu(1, 0, '/crm', component=None), menu(2, 1, 'customer'),
            menu(3, 2, permission='crm:customer:query', kind=3),
            menu(4, 2, permission='crm:customer:create', kind=3),
            menu(5, 0, '/ai'), menu(6, 5, 'model'),
            menu(7, 0, '/iot'), menu(8, 7, 'device'),
            menu(9, 0, 'https://www.iocoder.cn', name='作者动态'),
            menu(10, 0, component='infra/codegen/index'), menu(11, 10),
            menu(12, 1, 'create', permission='crm:customer:create'),
        ])
        self.assertEqual(scope['explorer_grants'], [1, 2, 3])
        self.assertEqual(scope['disable'], [7, 8])
        self.assertEqual(scope['hidden_from_explorer'], [5, 6, 10, 11])
        self.assertEqual(scope['remove'], [9])

    def test_unacknowledged_or_uncertain_writes_block_new_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / 'writes.jsonl'
            for event in [{'sequence': 1, 'state': 'dispatch'}, {'sequence': 1, 'state': 'unknown'}]:
                journal.write_text(json.dumps(event) + '\n')
                with self.assertRaises(RuntimeError):
                    common.DemoClient('http://localhost', '1', journal)

    def test_journal_does_not_contain_credentials_and_unknown_is_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / 'writes.jsonl'
            client = common.DemoClient('http://localhost', '1', journal)
            with patch.object(common.Client, 'call', side_effect=TimeoutError):
                with self.assertRaises(TimeoutError):
                    client.post('system/user/create', {'password': 'never-journal-this'})
            self.assertNotIn('never-journal-this', journal.read_text())
            self.assertEqual(json.loads(journal.read_text().splitlines()[-1])['state'], 'unknown')
            with self.assertRaises(RuntimeError):
                common.DemoClient('http://localhost', '1', journal)

    def test_repeated_page_cannot_be_misreported_as_complete_fixture(self):
        class Client:
            def get(self, *args, **kwargs):
                return {'list': [{'id': 1}], 'total': 2}
        with self.assertRaisesRegex(RuntimeError, 'Duplicate'):
            common.Records(Client()).rows('entities/page')

    def test_empty_page_before_declared_total_refuses_partial_index(self):
        class Client:
            def get(self, *args, **kwargs):
                return {'list': [], 'total': 10}
        with self.assertRaisesRegex(RuntimeError, 'Incomplete'):
            common.Records(Client()).rows('entities/page')

    def test_local_date_round_trip_does_not_duplicate_midnight_record(self):
        class Client:
            def get(self, *args, **kwargs):
                return {'list': [{'id': 1, 'date': 1792425600000}], 'total': 1}
            def post(self, *args, **kwargs):
                raise AssertionError('same-day existing record must not be recreated')
        self.assertEqual(common.Records(Client()).ensure('holidays',
            {'date': 1792461600000}, key='date'), 1)

    def test_same_name_different_target_does_not_authorize_deletion(self):
        scope = common.menu_scope([dict(id=1, parentId=0, name='作者动态',
            path='https://example.invalid', component=None, type=2, status=0)])
        self.assertEqual(scope['remove'], [])

    def test_personal_setup_revokes_temporary_permissions_on_failure(self):
        class Client:
            base = 'http://localhost'
            def __init__(self):
                self.calls = []
            def get(self, path):
                return [dict(id=i, permission='oa:' + part + ':create')
                        for i, part in enumerate(['schedule', 'plan', 'work-report'])]
            def post(self, path, body):
                self.calls.append((path, body))
        class Records:
            def __init__(self):
                self.c = Client()
            def ensure(self, *args, **kwargs):
                return 99
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / 'identity.json').write_text(json.dumps({'role': 7}))
            records = Records()
            with patch.object(demo_data, 'DemoClient', side_effect=RuntimeError('dispatch unavailable')):
                with self.assertRaises(RuntimeError):
                    demo_data.personal(records, state, 8, 3)
            self.assertEqual(records.c.calls[-2][1], {'userId': 8, 'roleIds': [7]})
            self.assertEqual(records.c.calls[-1][1], {'roleId': 99, 'menuIds': []})

    def test_coverage_setup_grants_only_declared_creates_and_revokes_on_failure(self):
        allowed = ['oa:' + name + ':create' for name in ['leave-apply', 'overtime-apply',
            'regular-apply', 'resign-apply', 'meeting-room-booking', 'vehicle-apply',
            'seal-apply', 'supply-apply', 'discussion']] + ['oa:travel-apply:save',
            'crm:clue:create', 'crm:contact:create', 'crm:business:create', 'crm:contract:create']
        class Client:
            base = 'http://localhost'
            def __init__(self):
                self.calls = []
            def get(self, path):
                return [dict(id=i, permission=p) for i, p in enumerate(allowed + ['system:user:delete'])]
            def post(self, path, body):
                self.calls.append((path, body))
        class Records:
            def __init__(self):
                self.c = Client()
            def ensure(self, *args, **kwargs):
                return 99
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / 'identity.json').write_text(json.dumps({'role': 7}))
            records = Records()
            with patch.object(demo_data, 'DemoClient', side_effect=RuntimeError('unavailable')):
                with self.assertRaises(RuntimeError):
                    demo_data.personal(records, state, 8, 3, coverage=True)
            self.assertEqual(records.c.calls[0][1]['menuIds'], list(range(len(allowed))))
            self.assertEqual(records.c.calls[-2][1], {'userId': 8, 'roleIds': [7]})
            self.assertEqual(records.c.calls[-1][1], {'roleId': 99, 'menuIds': []})

    def test_finance_reports_are_retained_when_standalone_report_module_is_disabled(self):
        rows = [dict(id=1, parentId=0, name='报表管理', path='/report', component=None, type=1, status=0),
                dict(id=2, parentId=0, name='FMS 财务管理', path='/fms', component=None, type=1, status=0),
                dict(id=3, parentId=2, name='报表管理', path='report', component=None, type=1, status=0)]
        scope = common.menu_scope(rows)
        self.assertEqual(scope['disable'], [1])
        self.assertEqual(scope['explorer_grants'], [2, 3])

    def test_server_error_does_not_allow_another_write_in_the_same_process(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / 'writes.jsonl'
            client = common.DemoClient('http://localhost', '1', journal)
            with patch.object(common.Client, 'call', side_effect=common.ApiError('create', 500, 'failed')):
                with self.assertRaises(common.ApiError):
                    client.post('create', {})
                with self.assertRaises(RuntimeError):
                    client.post('create', {})
            self.assertEqual(sum(json.loads(x)['state'] == 'dispatch'
                                 for x in journal.read_text().splitlines()), 1)


if __name__ == '__main__':
    unittest.main()
