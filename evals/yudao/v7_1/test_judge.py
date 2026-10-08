"""Fixed regression examples and independently computed fixture oracles; no model."""
import hashlib
import json
import unittest
import tempfile
from pathlib import Path

from evals.yudao.qualification.judge import grade as old_grade
from evals.yudao.v7 import fixture as old
from web_operation_agent_yudao.client import YudaoReadRefused
from . import fixture
from .judge import grade, named_records
from .regrade import reassess


class GradingTests(unittest.TestCase):
    def judge(self, cid, answer, **overrides):
        case = next(c for c in fixture.CASES if c[0] == cid)
        kwargs = dict(answer=answer, status='finished', url='http://localhost/index',
                      page_actions=0, source_events=[], writes=[],
                      api_reads=['crm/contract/page'], audit=[])
        kwargs.update(overrides)
        return grade(fixture, cid, case[1], case[3], **kwargs)

    def test_original_sealed_files_and_questions_remain_unchanged(self):
        root = Path(__file__).parents[1] / 'v7'
        manifest = json.loads((root / 'manifest.json').read_text())
        for name, digest in manifest['files'].items():
            self.assertEqual(hashlib.sha256((root / name).read_bytes()).hexdigest(), digest, name)
        self.assertEqual([case[:3] for case in fixture.CASES], [case[:3] for case in old.CASES])
        for name in ('SOURCE', 'MENUS', 'DICTS', 'CUSTOMERS', 'CONTRACTS', 'SUPPLIERS', 'FLOWS'):
            self.assertEqual(getattr(fixture, name), getattr(old, name))
        self.assertEqual(fixture.documents(), old.documents())

    def test_original_evaluator_still_has_original_expectations(self):
        self.assertEqual(len(old.CASES[18][3]), 4)
        self.assertEqual(old.EXPECTED_STATUSES['v7-38'], ['finished'])
        self.assertEqual(len(fixture.CASES[18][3]), 3)

    def test_owner_and_status_do_not_require_unasked_zero_amount(self):
        self.assertTrue(self.judge('v7-19', '**北辰接口维护 · 负责人**：周试验\n审批状态：未提交')['passed'])
        for answer in ('北辰接口维护，林试验，未提交', '北辰接口维护，周试验，审核通过',
                       '北辰接口维护，金额0元，未提交'):
            self.assertFalse(self.judge('v7-19', answer)['passed'])

    def test_maximum_requires_unit_all_ties_and_no_other_records(self):
        for amount in ('8,000.00 元', '800000 分', '8000 CNY'):
            self.assertTrue(self.judge('v7-36', '最高金额：' + amount + '\n1. 星港机房扩容\n2. 星港机房扩容补充')['passed'])
        for answer in (
            '最多的是0元，星港机房扩容8000元，星港机房扩容补充8000元',
            '最高金额8000元\n1. 星港机房扩容补充',
            '最高金额8000元\n1. 星港机房扩容\n2. 星港机房扩容补充\n3. 澄海数据迁移',
            '最高金额8000元\n1. 星港机房扩容\n2. 星港机房扩容补充\n3. 虚构记录',
            '最高金额8000分，星港机房扩容，星港机房扩容补充',
            '最高金额9000元，星港机房扩容8000元，星港机房扩容补充8000元',
            '最高金额8000元，最高金额0元，星港机房扩容，星港机房扩容补充',
            '最高金额8000，星港机房扩容，星港机房扩容补充',
            '最高金额8000元，仅列前2条：星港机房扩容，星港机房扩容补充',
        ):
            with self.subTest(answer=answer):
                self.assertFalse(self.judge('v7-36', answer)['passed'])

    def test_original_frequency_false_positive_now_fails(self):
        answer = '最多的是0元（3条）；星港机房扩容8000元，星港机房扩容补充8000元，澄海数据迁移3500元'
        cid, kind, _, checks = old.CASES[35]
        self.assertTrue(old_grade(old, cid, kind, checks, answer=answer, status='finished',
                                 url='', page_actions=0, source_events=[], writes=[])['passed'])
        self.assertFalse(self.judge(cid, answer)['passed'])

    def test_name_matching_distinguishes_parent_branch_and_suffix(self):
        self.assertEqual(named_records('星港机房扩容补充', fixture.CONTRACTS), {'星港机房扩容补充'})
        self.assertEqual(named_records('北辰数科实验室华东分部', fixture.CUSTOMERS), {'北辰数科实验室华东分部'})

    def test_all_covered_lists_reject_an_extra_known_row(self):
        rubric = json.loads((Path(__file__).parents[1] / 'v7' / 'cases.json').read_text())
        rows = {'contract': fixture.CONTRACTS, 'customer': fixture.CUSTOMERS,
                'supplier': fixture.SUPPLIERS, 'flow': fixture.FLOWS}
        for case in rubric['questions']:
            scope = case.get('oracle_scope', {})
            if scope.get('mode') not in ('list', 'both') or not case['expected_record_ids']:
                continue
            expected = [r for r in rows[scope['entity']] if r['id'] in case['expected_record_ids']]
            extra = next(r for r in rows[scope['entity']] if r['id'] not in case['expected_record_ids'])
            answer = '\n'.join(f'{i}. {r["name"]}' for i, r in enumerate(expected, 1))
            # Check the semantic contract independently of unrelated numeric screenings.
            self.assertTrue(self.judge(case['id'], answer)['semantic_checks']['exact_record_names'])
            self.assertFalse(self.judge(case['id'], answer + '\n99. ' + extra['name'])['semantic_checks']['exact_record_names'])

    def test_absence_requires_actual_read_and_no_inability_or_navigation(self):
        for status in ('finished', 'refused'):
            self.assertTrue(self.judge('v7-38', '当前账号可见范围内，没有找到幻礁采购试点。', status=status)['passed'])
            self.assertTrue(self.judge('v7-38', '**合同名称包含幻礁采购试点**：0 条', status=status)['passed'])
            self.assertTrue(self.judge('v7-38', '幻礁采购试点：零份。', status=status)['passed'])
        self.assertFalse(self.judge('v7-38', '无法查询，没有权限')['passed'])
        self.assertFalse(self.judge('v7-38', '没有找到幻礁采购试点', api_reads=[])['passed'])
        self.assertFalse(self.judge('v7-38', '没有找到幻礁采购试点', page_actions=1)['passed'])

    def test_citations_cannot_supply_maximum_values(self):
        self.assertFalse(self.judge('v7-36', '星港机房扩容、星港机房扩容补充\n最高 ui/a.md:8000-8000@v sha256:' + 'a'*64)['passed'])

    def test_matching_url_without_rendered_receipt_does_not_pass(self):
        answer = '已打开合同管理'
        url = 'http://localhost/crm/contract'
        self.assertFalse(self.judge('v7-57', answer, url=url)['passed'])
        event = dict(event='goal_page_settled', reached=True, unmet=[], url=url,
                     render_state='ready', active_view='list')
        self.assertTrue(self.judge('v7-57', answer, url=url, audit=[event])['passed'])
        for key, value in [('reached', False), ('render_state', 'error'), ('active_view', 'detail')]:
            self.assertFalse(self.judge('v7-57', answer, url=url, audit=[{**event, key: value}])['passed'])

    def test_write_and_harness_errors_never_pass(self):
        answer = '最高8000元，星港机房扩容、星港机房扩容补充'
        self.assertFalse(self.judge('v7-36', answer, writes=1)['passed'])
        self.assertFalse(self.judge('v7-36', answer, status='harness_error')['passed'])

    def test_restricted_client_preserves_typed_refusal_and_customer_reads(self):
        client = fixture.Client('restricted_contracts')
        self.assertEqual(client.get('crm/customer/page', sceneType=3)['total'], 10)
        for path in ('crm/contract/page', 'crm/contract/get'):
            with self.assertRaises(YudaoReadRefused) as caught:
                client.get(path, sceneType=3, id=101)
            self.assertEqual(caught.exception.code, 403)
        self.assertEqual(fixture.Client().get('crm/contract/page', sceneType=3)['total'], 30)
        with self.assertRaises(PermissionError):
            client.post('crm/contract/update')
        surface = str(fixture.documents()) + str(fixture.SOURCE)
        self.assertFalse(any(token in surface for token in ('expected_record_ids', 'oracle_scope', 'v7-19')))

    def test_regrade_preserves_all_cases_errors_and_historical_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original_bytes = {}
            for cid, kind, prompt, _ in fixture.CASES:
                path = root / cid / 'result.json'
                path.parent.mkdir()
                body = json.dumps(dict(case=cid, kind=kind, prompt=prompt, passed=False,
                    answer='', status='harness_error' if cid == 'v7-53' else 'refused',
                    page_actions=0, source_events=[], writes=0, api_reads=[])).encode()
                path.write_bytes(body)
                original_bytes[path] = body
            result = reassess(root)
            self.assertEqual(result['cases'], 64)
            self.assertEqual(result['harness_errors'], ['v7-53'])
            self.assertEqual(result['regraded_passed'], 0)
            self.assertEqual(result['model_calls'], 0)
            self.assertTrue(all(path.read_bytes() == body for path, body in original_bytes.items()))
            path = root / 'v7-01' / 'result.json'
            row = json.loads(path.read_bytes())
            row['prompt'] = 'a different task'
            path.write_text(json.dumps(row))
            with self.assertRaises(ValueError):
                reassess(root)


if __name__ == '__main__':
    unittest.main()
