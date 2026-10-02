import unittest
from evals.yudao.v7_1 import fixture
from .judge import grade


class SeparateScopeTests(unittest.TestCase):
    def judge(self,answer,status='finished'):
        cid,kind,_,checks=fixture.CASES[28]
        return grade(fixture,cid,kind,checks,answer=answer,status=status,url='',page_actions=0,
                     source_events=[],writes=0,api_reads=['bpm/process-instance/my-page'],audit=[])

    def test_total_and_resubmission_subset_or_full_list_pass(self):
        for answer in ('我的流程中名称含出差申请：2条。重报记录：研发出差申请重报。',
                       '出差申请共2条：研发出差申请；研发出差申请重报。'):
            self.assertTrue(self.judge(answer)['passed'],answer)

    def test_wrong_count_omission_unrelated_record_and_clarification_fail(self):
        for answer in ('共1条：研发出差申请重报。','共2条：研发出差申请。',
                       '共2条：研发出差申请重报；' + fixture.FLOWS[-1]['name'] + '。'):
            self.assertFalse(self.judge(answer)['passed'],answer)
        self.assertFalse(self.judge('共2条：研发出差申请重报。','needs_input')['passed'])
