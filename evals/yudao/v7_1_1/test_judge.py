"""Generic inability must not count as an explicit actor/target permission answer."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from evals.yudao.v7_1 import fixture
from web_operation_agent_yudao.client import YudaoReadRefused
from .judge import grade
from .regrade import reassess


class PermissionGradingTests(unittest.TestCase):
    def judge(self, answer):
        cid, kind, _, checks = fixture.CASES[52]
        return grade(fixture, cid, kind, checks, answer=answer, status='refused',
                     url='', page_actions=0, source_events=[], writes=0,
                     api_reads=['system/auth/get-permission-info','crm/customer/page'], audit=[])

    def test_explicit_permission_and_readable_count_pass(self):
        for text in ('你的账号没有读取合同的权限，这一项没有完成。客户管理：10条。',
                     '合同权限不足，客户共10个。', '无权查询合同；客户数量：10。',
                     '合同未授权；客户共十个。'):
            self.assertTrue(self.judge(text)['passed'], text)

    def test_generic_or_wrong_target_refusal_and_missing_count_fail(self):
        for text in ('无法完成这一项。客户管理：10条。', '合同无法完成。客户管理：10条。',
                     '没有权限查询客户。客户管理：10条。', '合同无权查询，客户未知。',
                     '合同无权查询。客户共100个。', '合同无权查询。客户共11个。',
                     '合同无权查询，合同共有10份；客户未知。'):
            self.assertFalse(self.judge(text)['passed'], text)

    def test_adapter_maps_only_typed_permission_code(self):
        self.assertTrue(YudaoReadRefused('opaque', code=403).permission_denied)
        for code in (401, 500, None, '403', True):
            self.assertFalse(YudaoReadRefused('403 permission denied', code=code).permission_denied)

    def test_regrade_adjusts_parent_total_and_replaces_parent_change(self):
        row = dict(answer='无法完成这一项。客户管理：10条。', status='refused',
                   page_actions=0, source_events=[], writes=0, api_reads=[], passed=False)
        parent = dict(regraded_passed=64, failures=[], grader_binding={},
                      changes=[dict(case='v7-53', old_passed=False, new_passed=True)])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'v7-53').mkdir()
            (root/'v7-53/result.json').write_text(json.dumps(row))
            with patch('evals.yudao.v7_1_1.regrade.parent_reassess', return_value=parent):
                result = reassess(root)
        self.assertEqual(result['regraded_passed'], 63)
        self.assertEqual(result['failures'], ['v7-53'])
        self.assertEqual(result['changes'], [])


if __name__ == '__main__':
    unittest.main()
