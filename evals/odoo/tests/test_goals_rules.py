"""Rules grading separates a correct answer from byte-verified source evidence."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from collections import Counter

from evals.odoo.goals.judge import _check, score
from evals.odoo.goals.run_goals import load_tasks, TASKSETS
from evals.odoo.goals.source_citations import CitationSource, bound_source


class SourceRuleJudge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = 'addons/example/models/order.py'
        self.raw = b'def confirm():\n    if locked:\n        raise UserError("Cannot cancel")\n'
        p = self.root / self.path
        p.parent.mkdir(parents=True)
        p.write_bytes(self.raw)
        self.files = {self.path: hashlib.sha256(self.raw).hexdigest()}
        self.source = CitationSource(self.root, releases=['test-release'], files=self.files)
        self.check = {'answer_regex': 'locked',
                      'source_regex': r'(?m)^addons/example/models/order\.py:2@test-release$'}
        self.task = {'parts': [{'quote': 'When is cancellation refused?', 'check': self.check}]}

    def citation(self, start=1, end=3, release='test-release', digest=None):
        digest = digest or hashlib.sha256(b''.join(self.raw.splitlines(keepends=True)[start-1:end])).hexdigest()
        return f'{self.path}:{start}-{end}@{release} sha256:{digest}'

    def verdict(self, answer, **kwargs):
        return score(self.task, probe={}, visited=[], answer=answer, finished=True,
                     write_detected=kwargs.get('write_detected', False),
                     source=kwargs.get('source', self.source))

    def test_three_arms_can_agree_on_answer_but_only_verified_sources_pass(self):
        plain = 'locked means cancellation is refused'
        metadata = self.verdict(plain)
        self.assertEqual(metadata.answer_checks, [True])
        self.assertEqual(metadata.source_checks, [False])
        self.assertFalse(metadata.success)
        # A compiled element and a broader generic-retrieval block cover the same rule.
        for citation in (self.citation(2, 2), self.citation(1, 3)):
            verdict = self.verdict(plain + '\n```source\n' + citation + '\n```')
            self.assertTrue(verdict.success)
            self.assertEqual(verdict.source_checks, [True])
        self.assertFalse(self.verdict(plain + self.citation(), source=None).success)

    def test_right_filename_without_hash_or_with_wrong_hash_release_or_lines_fails(self):
        for reference in (self.path, self.citation().split(' sha256:')[0],
                          self.citation(digest='0' * 64), self.citation(release='other'),
                          self.citation(1, 1), self.citation().replace(':1-3@', ':3-1@'),
                          self.citation().replace(':1-3@', ':1-999@'),
                          self.citation().replace(':1-3@', ':' + '9' * 5000 + '-3@')):
            with self.subTest(reference=reference):
                self.assertFalse(self.verdict('locked ' + reference).success)

    def test_rule_refusal_word_is_not_assistant_failure_and_write_still_fails(self):
        answer = 'locked 的订单不能取消。\n' + self.citation()
        self.assertTrue(self.verdict(answer).success)
        self.assertFalse(self.verdict(answer, write_detected=True).success)
        self.assertFalse(score(self.task, probe={}, visited=[], answer=answer,
                               finished=False, write_detected=False, source=self.source).success)
        self.assertFalse(self.verdict('wrong answer ' + self.citation()).success)

    def test_source_change_does_not_reuse_a_cached_verification(self):
        citation = self.citation()
        (self.root / self.path).write_bytes(self.raw + b'# changed\n')
        self.assertFalse(self.verdict('locked ' + citation).success)
        with self.assertRaises(ValueError):
            CitationSource(self.root, releases=['test-release'], files=self.files)

    def test_path_escape_unsealed_file_and_oversized_reply_fail_closed(self):
        for path in ('../' + self.path, '/' + self.path, 'other.py'):
            answer = 'locked ' + self.citation().replace(self.path, path)
            self.assertFalse(self.verdict(answer).success)
        self.assertFalse(self.verdict('locked ' + 'x' * 200_001 + self.citation()).success)
        self.assertFalse(self.verdict('locked\n' + (self.citation() + '\n') * 65).success)
        outside = self.root.parent / (self.root.name + '-outside.py')
        outside.write_bytes(self.raw)
        self.addCleanup(outside.unlink)
        (self.root / self.path).unlink()
        (self.root / self.path).symlink_to(outside)
        self.assertFalse(self.verdict('locked ' + self.citation()).success)

    def test_release_binding_and_newline_hash_are_exact(self):
        binding = {'source_binding': {'deployment_release': 'full-release',
                   'citation_releases': ['test-release'], 'files': self.files}}
        with self.assertRaises(ValueError):
            bound_source(binding, self.root, 'different-release')
        self.assertIsNone(bound_source({}, self.root, 'anything'))
        crlf_hash = hashlib.sha256(self.raw.replace(b'\n', b'\r\n')).hexdigest()
        self.assertFalse(self.verdict('locked ' + self.citation(digest=crlf_hash)).success)

    def test_wrong_binding_stops_runner_before_database_or_model_dispatch(self):
        from evals.odoo.goals.run_goals import run_attempt
        doc = {'source_binding': {'deployment_release': 'expected-release',
               'citation_releases': ['test-release'], 'files': self.files}}
        with patch('evals.odoo.goals.run_goals.run_demo') as model, \
             patch('evals.odoo.goals.run_goals.fingerprint') as database:
            with self.assertRaises(ValueError):
                run_attempt({}, doc, env={'WOA_ODOO_SOURCE_ROOT': str(self.root),
                            'WOA_ODOO_SOURCE_RELEASE': 'wrong-release'},
                            credentials=self.root, output=self.root, model_env=self.root,
                            container='unused', database='unused')
            model.assert_not_called()
            database.assert_not_called()

    def test_absent_rule_needs_specific_explanation_not_generic_cannot(self):
        task = {'parts': [{'check': {'impossible': True,
                 'explains_regex': r'(?s)(?=.*daily cap)(?=.*not found)'}}]}
        for answer, finished, expected in (
            ('cannot answer', True, False),
            ('daily cap rule not found; cannot infer it', True, True),
            ('daily cap rule not found; cannot infer it', False, False),
            ('', True, False), ('There is a daily cap.', True, False),
        ):
            self.assertEqual(score(task, probe={}, visited=[], answer=answer,
                                   finished=finished, write_detected=False).success, expected)

    def test_nested_any_of_cannot_bypass_source_check_in_its_branch(self):
        check = {'any_of': [self.check]}
        self.assertFalse(_check(check, {}, [], 'locked', source=self.source))
        self.assertTrue(_check(check, {}, [], 'locked ' + self.citation(), source=self.source))


class FrozenRules(unittest.TestCase):
    def test_seal_distribution_provenance_and_prompt_separation(self):
        doc = load_tasks('v6-rules')
        path, digest = TASKSETS['v6-rules']
        seal = json.loads(path.with_suffix('.seal.json').read_text())
        self.assertEqual(seal['task_set_sha256'], 'sha256:' + digest)
        self.assertEqual(doc['reference_date'], seal['frozen_at'][:10])
        self.assertEqual(doc['source_binding']['release'], '18.0-20260909')
        self.assertEqual(Counter(t['condition'] for t in doc['tasks']), {
            'visibility': 8, 'readonly': 8, 'controls': 6, 'refusal': 8,
            'meaning': 5, 'absent_rule': 5})
        previous = {t['prompt'] for v in ('v4', 'v5') for t in load_tasks(v)['tasks']}
        self.assertEqual(len({t['prompt'] for t in doc['tasks']}), 40)
        self.assertFalse(previous & {t['prompt'] for t in doc['tasks']})
        self.assertEqual({x['id'] for x in seal['verified_tasks']},
                         {x['id'] for x in doc['tasks']})
        for task in doc['tasks']:
            self.assertTrue(task['truth']['sources'])
            for part in task['parts']:
                self.assertIn(part['quote'], task['prompt'])
                check = part['check']
                if task['category'] == 'impossible':
                    self.assertTrue(check['impossible'])
                    self.assertIn('explains_regex', check)
                else:
                    self.assertTrue({'answer_regex', 'source_regex'} <= check.keys())
                    self.assertNotIn('部署', check['answer_regex'])
                    self.assertFalse(_check({'source_regex': check['source_regex']}, {}, [],
                                            task['prompt']))
                for key, pattern in check.items():
                    if key.endswith('_regex'):
                        re.compile(pattern)


if __name__ == '__main__':
    unittest.main()
