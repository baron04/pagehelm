"""Prevent grading false positives; no executor or paid calls."""
import hashlib
import unittest
from types import SimpleNamespace

from .judge import grade, verify_citations


class JudgeTests(unittest.TestCase):
    def setUp(self):
        self.source={'ui/docs/team.md':'维护团队：银河组。\n'}
        digest=hashlib.sha256(self.source['ui/docs/team.md'].encode()).hexdigest()
        self.citation=f'ui/docs/team.md:1-1@synthetic-v7-1 sha256:{digest}'
        self.fixture=SimpleNamespace(SOURCE=self.source,RELEASE='synthetic-v7-1',
            PAGE_PATHS={'page':'/crm/contract'},EXPECTED_STATUSES={},FORBIDDEN={},REQUIRES_SOURCE={'source'})

    def judge(self,cid,kind,checks,answer,**overrides):
        kwargs=dict(answer=answer,status='finished',url='http://localhost/crm/contract',
                    page_actions=1,source_events=[],writes=[])
        kwargs.update(overrides)
        return grade(self.fixture,cid,kind,checks,**kwargs)

    def test_page_route_never_overwrites_missing_fact_failure(self):
        self.assertFalse(self.judge('page','page',['银河组'],'已打开')['passed'])
        self.assertTrue(self.judge('page','page',['银河组'],'银河组')['passed'])

    def test_source_bytes_and_displayed_evidence_are_both_required(self):
        event={'complete':True,'citations':[self.citation]}
        self.assertTrue(self.judge('source','source',['银河组'],'银河组\n'+self.citation,source_events=[event])['passed'])
        self.assertFalse(self.judge('source','source',['银河组'],'银河组',source_events=[event])['passed'])
        self.assertFalse(verify_citations(self.citation.replace('1-1','1-2'),self.source,'synthetic-v7-1'))
        self.assertFalse(verify_citations(self.citation.replace('v7-1','v7-2'),self.source,'synthetic-v7-1'))

    def test_citation_line_numbers_cannot_satisfy_answer_numbers(self):
        self.assertFalse(self.judge('data','data',[r'(?<!\d)1(?!\d)'],self.citation)['passed'])

    def test_partial_business_answer_and_ambiguity_navigation_do_not_pass(self):
        self.assertFalse(self.judge('data','data',['银河组'],'银河组',status='partial')['passed'])
        self.fixture.EXPECTED_STATUSES={'clarify':['needs_input']}
        self.assertFalse(self.judge('clarify','clarification',[],'请选择',status='needs_input')['passed'])
        self.assertTrue(self.judge('clarify','clarification',[],'请选择',status='needs_input',page_actions=0)['passed'])


if __name__ == '__main__':
    unittest.main()
