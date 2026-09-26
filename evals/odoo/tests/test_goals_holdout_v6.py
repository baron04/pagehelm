"""Offline structural checks for the v6 freeze; the live verifier is separate."""
from __future__ import annotations

from collections import Counter
import difflib
import hashlib
import json
from pathlib import Path
import re
import unittest

from evals.odoo.goals.probe import FINAL_STATE_PROBE
from evals.odoo.goals.run_goals import TASKSETS, load_tasks


class V6Freeze(unittest.TestCase):
    def test_distribution_and_oracle_contract(self):
        doc = load_tasks('v6')
        tasks = doc['tasks']
        self.assertEqual(len(tasks), 100)
        self.assertEqual(Counter(t['group'] for t in tasks), {'A':56,'B':28,'C':6,'D':10})
        a = [t for t in tasks if t['group'] == 'A']
        self.assertEqual(Counter(t['category'] for t in a),
                         {'answer':34,'open':6,'handoff':10,'impossible':6})
        self.assertEqual(Counter(t['delivery'] for t in a if t['category']=='answer'),
                         {'answer/field':10,'answer/list':10,'answer/lines':8,
                          'answer/aggregate':6})
        self.assertEqual(Counter(t['condition'] for t in tasks if t['group']=='B'),
                         {'visibility':6,'readonly':6,'refusal':6,'controls':4,
                          'meaning':3,'absent_rule':3})
        self.assertEqual(Counter(t['category'] for t in tasks if t['group']=='D'),
                         {'answer':8,'open':1,'handoff':1})
        self.assertEqual(sum(t['delivery']=='answer/rule' for t in tasks if t['group']=='D'), 4)
        self.assertEqual(sum(t['delivery']=='handoff/field' for t in a), 4)
        self.assertEqual(sum(t['delivery']=='handoff/button' for t in a), 6)
        self.assertEqual(doc['reference_date'], '2026-09-26')
        self.assertEqual(doc['timezone'], 'Asia/Shanghai')
        self.assertEqual(doc['source_binding']['release'], '18.0-20260909')
        self.assertEqual(len({t['id'] for t in tasks}), len(tasks))
        for task in tasks:
            self.assertTrue(task['prompt'])
            for part in task['parts']:
                check = part['check']
                for key, value in check.items():
                    if key.endswith('_regex'):
                        re.compile(value)
                        self.assertFalse(any(ord(c)<32 or ord(c)==127 for c in value))
                if task['delivery'] in ('answer/rule','answer/mixed') and 'source_regex' in check:
                    self.assertIn('answer_regex', check)
                if task['category']=='impossible':
                    self.assertTrue(check['impossible'])
                    self.assertIn('explains_regex', check)

    def test_no_repeated_or_close_development_prompts(self):
        tasks = load_tasks('v6')['tasks']
        previous = [t['prompt'] for v in ('v5','v6-rules')
                    for t in load_tasks(v)['tasks']]
        self.assertFalse({t['prompt'] for t in tasks} & set(previous))
        self.assertEqual(len({t['prompt'] for t in tasks}), len(tasks))
        for task in tasks:
            similarity = max(difflib.SequenceMatcher(None, task['prompt'], old).ratio()
                             for old in previous)
            self.assertLess(similarity, 0.7, task['id'])

    def test_numeric_boundaries_and_value_probe(self):
        tasks = {t['id']:t for t in load_tasks('v6')['tasks']}
        pattern = tasks['afield02']['parts'][0]['check']['answer_regex']
        self.assertRegex('1541.50', pattern)
        self.assertNotRegex('541.50', pattern)
        self.assertNotRegex('11541.50', pattern)
        self.assertNotRegex('1541.500', pattern)
        self.assertIn('input.value', FINAL_STATE_PROBE)
        self.assertIn('value:', FINAL_STATE_PROBE)

    def test_development_regex_correction_and_qualification_record(self):
        old = load_tasks('v6-rules')
        visibility = next(t for t in old['tasks'] if t['id']=='visibility03')
        self.assertIn(r'\bid\b', visibility['parts'][0]['check']['answer_regex'])
        self.assertNotIn('\x08', visibility['parts'][0]['check']['answer_regex'])
        task_path, task_hash = TASKSETS['v6']
        report_path = task_path.with_suffix('.qualification.json')
        report = json.loads(report_path.read_text())
        seal = json.loads(task_path.with_suffix('.seal.json').read_text())
        self.assertEqual(report['task_set_sha256'], task_hash)
        self.assertEqual(seal['task_set_sha256'], 'sha256:' + task_hash)
        self.assertEqual(seal['qualification_sha256'], 'sha256:' + hashlib.sha256(
            report_path.read_bytes()).hexdigest())
        self.assertEqual(seal['fixture_signature'], report['fixture_signature'])
        self.assertEqual(report['model_calls'], 0)
        self.assertEqual(report['tasks'], 100)


if __name__=='__main__':
    unittest.main()
