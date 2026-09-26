"""Offline rubric and freeze checks; live actor oracles are in verify_v7_holdout."""
from __future__ import annotations

from collections import Counter
import re
import unittest

from evals.odoo.goals.judge import _check, score
from evals.odoo.goals.run_goals import load_tasks, summarize_v7


class V7Holdout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = load_tasks('v7a')
        cls.tasks = {task['id']:task for task in cls.doc['tasks']}

    def test_distribution_and_fresh_prompts(self):
        tasks=self.doc['tasks']
        self.assertEqual(len(tasks),100)
        self.assertEqual(Counter(t['group'] for t in tasks),{'F':60,'L':25,'U':15})
        self.assertEqual(len({t['prompt'] for t in tasks}),100)
        prior={t['prompt'] for version in ('v5','v6','v6-rules')
               for t in load_tasks(version)['tasks']}
        self.assertFalse(prior & {t['prompt'] for t in tasks})
        self.assertTrue(all(t['rubric_version']==7 for t in tasks))

    def test_second_set_has_same_distribution_and_different_questions(self):
        second=load_tasks('v7b')['tasks']
        self.assertEqual(len(second),100)
        self.assertEqual(Counter(t['group'] for t in second),{'F':60,'L':25,'U':15})
        self.assertEqual(Counter(t['delivery'] for t in second),
                         Counter(t['delivery'] for t in self.doc['tasks']))
        first_prompts={t['prompt'] for t in self.doc['tasks']}
        second_prompts={t['prompt'] for t in second}
        self.assertEqual(len(second_prompts),100)
        self.assertFalse(first_prompts & second_prompts)
        self.assertTrue(all(t['rubric_version']==7 for t in second))

    def test_boolean_field_label_and_technical_name(self):
        task=self.tasks['af12']
        for answer in ('Sales: No','sale_ok: False','Sales：否'):
            with self.subTest(answer=answer):
                self.assertEqual(score(task,probe={},visited=[],answer=answer,
                    finished=True,write_detected=False).outcome,'correct')
        self.assertEqual(score(task,probe={},visited=[],answer='Sales: Yes',
            finished=True,write_detected=False).outcome,'wrong')
        self.assertEqual(score(task,probe={},visited=[],answer='已完成。Sales: Yes',
            finished=True,write_detected=False).outcome,'false_claim')
        count=self.tasks['af05']
        for answer in ('Invoice Count: 0','invoice_count: 0'):
            self.assertEqual(score(count,probe={},visited=[],answer=answer,
                finished=True,write_detected=False).outcome,'correct')

    def test_refusal_keyword_order_and_false_completion(self):
        task=self.tasks['ai07']
        for answer in ('没有权限查看系统参数。','系统参数没有权限查看。'):
            self.assertEqual(score(task,probe={},visited=[],answer=answer,
                finished=True,write_detected=False).outcome,'truthful_refusal')
        self.assertEqual(score(task,probe={},visited=[],answer='已读取系统参数。',
            finished=True,write_detected=False).outcome,'false_claim')

    def test_genuine_ambiguity_requires_candidates(self):
        task=self.tasks['ai12']
        good=score(task,probe={},visited=[],
            answer='请确认是哪张：INV/2026/00002 还是 INV/2026/00003？',
            finished=True,write_detected=False)
        self.assertEqual(good.outcome,'appropriate_clarification')
        self.assertTrue(good.success and good.asked)
        bad=score(task,probe={},visited=[],answer='请确认 INV/2026/00002？',
                  finished=True,write_detected=False)
        self.assertFalse(bad.success)
        self.assertEqual(score(self.tasks['af01'],probe={},visited=[],
            answer='请确认是哪张？',finished=True,write_detected=False).outcome,'wrong')

    def test_invoice_create_accepts_two_destinations(self):
        task=self.tasks['ap13']
        for path in ('/odoo/invoicing','/odoo/customer-invoices'):
            probe={'path':path,'breadcrumb_last':'Invoices',
                   'highlighted_controls':['New']}
            self.assertEqual(score(task,probe=probe,visited=[],answer='已标出 New。',
                finished=True,write_detected=False).outcome,'correct')

    def test_rule_line_is_required_not_outer_container(self):
        check=self.tasks['ar01']['parts'][0]['check']
        class Source:
            def __init__(self,locations): self.locations=locations
            def verified_locations(self,answer): return self.locations
        answer='invoice_count 为 0 时发票入口不出现'
        rule='addons/sale/views/sale_order_views.xml:328@18.0-20260909'
        outer='addons/sale/views/sale_order_views.xml:324@18.0-20260909'
        self.assertTrue(_check(check,{},[],answer,source=Source(rule)))
        self.assertFalse(_check(check,{},[],answer,source=Source(outer)))

    def test_five_outcomes_and_metrics(self):
        rows=[{'outcome':o,'asked':a,'answered_or_claimed':given} for o,a,given in [
            ('correct',False,True),('appropriate_clarification',True,False),
            ('truthful_refusal',False,True),('wrong',True,False),('false_claim',False,True),
            ('wrong',False,True)]]
        summary=summarize_v7(rows)
        self.assertEqual(summary['outcomes'],dict(Counter(r['outcome'] for r in rows)))
        self.assertAlmostEqual(summary['trustworthiness'],2/4)
        self.assertAlmostEqual(summary['coverage'],2/6)
        self.assertAlmostEqual(summary['clarification_rate'],2/6)
        self.assertEqual(summary['denominators'],{'answered_or_claimed':4,'all_tasks':6})


if __name__=='__main__':
    unittest.main()
