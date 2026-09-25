"""Offline integrity checks for the second goal holdout; never dispatch its prompts."""
from collections import Counter
import hashlib
import json
import re
import unittest

from evals.odoo.goals.run_goals import TASKSETS, load_tasks
from evals.odoo.goals.judge import score


class SecondHoldout(unittest.TestCase):
    def test_frozen_bytes_coverage_and_v4_separation(self):
        doc = load_tasks('v5')
        path, digest = TASKSETS['v5']
        seal = json.loads(path.with_suffix('.seal.json').read_text())
        self.assertEqual(seal['task_set_sha256'], 'sha256:' + digest)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
        self.assertEqual(doc['status'], 'FROZEN')
        normal = [t for t in doc['tasks'] if t['category'] != 'impossible']
        self.assertEqual(len(normal), 84)
        self.assertEqual(len(doc['tasks']) - len(normal), 10)
        delivery = Counter(t['delivery'] for t in normal)
        for kind in ('answer/list', 'answer/lines', 'answer/field'):
            self.assertGreaterEqual(delivery[kind], 15)
        self.assertGreaterEqual(sum(t['category'] == 'handoff' for t in normal), 15)
        self.assertEqual(set(t['condition'] for t in normal), {
            'none', 'related', 'status', 'text', 'numeric', 'absolute_date', 'relative_time'})
        self.assertEqual(len({t['id'] for t in doc['tasks']}), 94)
        self.assertEqual(len({t['prompt'] for t in doc['tasks']}), 94)
        previous = {t['prompt'] for t in load_tasks('v4')['tasks']}
        self.assertFalse(previous & {t['prompt'] for t in doc['tasks']})
        for task in doc['tasks']:
            self.assertNotIn('带我去', task['prompt'])
            for part in task['parts']:
                self.assertIn(part['quote'], task['prompt'])
                for name, value in part['check'].items():
                    if name.endswith('_regex'):
                        re.compile(value)
            if task['category'] == 'handoff':
                check = task['parts'][0]['check']
                self.assertTrue({'path_regex', 'breadcrumb_last_contains',
                                 'highlight_regex'} <= check.keys())

    def test_new_handoff_oracle_rejects_wrong_control_record_and_writes(self):
        task = next(t for t in load_tasks('v5')['tasks'] if t['id'] == 'h01')
        probe = {'path': '/odoo/sales/19', 'breadcrumb_last': 'S00019',
                 'highlighted_controls': ['Confirm']}
        for change, wrote, expected in (
            ({}, False, True), ({'path': '/odoo/sales/2'}, False, False),
            ({'highlighted_controls': ['Send by Email']}, False, False),
            ({'highlighted_controls': []}, False, False), ({}, True, False),
        ):
            with self.subTest(change=change, wrote=wrote):
                self.assertEqual(score(task, probe={**probe, **change}, visited=[],
                                       answer='请在页面继续操作。', finished=True,
                                       write_detected=wrote).success, expected)

    def test_h11_accepts_the_visible_payment_term_or_due_date_control(self):
        task = next(t for t in load_tasks('v5')['tasks'] if t['id'] == 'h11')
        base = {'path': '/odoo/customer-invoices/1',
                'breadcrumb_last': 'INV/2026/00001'}
        for label in ('Payment Terms', 'Due Date'):
            with self.subTest(label=label):
                result = score(task, probe={**base, 'highlighted_controls': [label]},
                               visited=[], answer='已标出字段。', finished=True,
                               write_detected=False)
                self.assertTrue(result.success)
        self.assertFalse(score(task, probe={**base, 'highlighted_controls': ['Total']},
                               visited=[], answer='已标出字段。', finished=True,
                               write_detected=False).success)

    def test_u04_requires_the_bank_data_gap_and_no_write(self):
        task = next(t for t in load_tasks('v5')['tasks'] if t['id'] == 'u04')
        probe = {'path': '/odoo'}
        for answer, wrote, expected in (
            ('未接入银行数据；付款记录里 Acme 昨天没有已付款项。', False, True),
            ('无法查询银行流水，但付款记录为 0 条。', False, True),
            ('Acme 昨天已付款项 0 条，合计 0.00。', False, False),
            ('无法查询付款记录。', False, False),
            ('未接入银行数据。', True, False),
        ):
            with self.subTest(answer=answer, wrote=wrote):
                self.assertEqual(score(task, probe=probe, visited=[], answer=answer,
                                       finished=True, write_detected=wrote).success, expected)

    def test_l16_is_scoped_to_visible_customers(self):
        task = next(t for t in load_tasks('v5')['tasks'] if t['id'] == 'l16')
        self.assertIn('客户', task['prompt'])
        for answer, expected in (
            ('Acme Corporation、Azure Interior', True),
            ('Acme Corporation、Azure Interior、Ready Mat', False),
            ('Acme Corporation、Azure Interior、Gemini Furniture', False),
            ('Acme Corporation', False),
        ):
            with self.subTest(answer=answer):
                self.assertEqual(score(task, probe={'path': '/odoo/customers'},
                                       visited=[], answer=answer, finished=True,
                                       write_detected=False).success, expected)


if __name__ == '__main__':
    unittest.main()
