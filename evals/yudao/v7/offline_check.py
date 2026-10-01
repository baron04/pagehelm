"""Zero-model v7 oracle and synthetic-surface consistency checks."""
import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('v7_fixture_offline', ROOT / 'fixture.py')
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)


def matching(rows, conditions):
    def accept(row):
        for field, op, val in conditions:
            x = row.get(field)
            result = {
                'eq': lambda: x == val,
                'contains': lambda: x is not None and val in x,
                'starts': lambda: x is not None and str(x).startswith(val),
                'ge': lambda: x is not None and x >= val,
                'gt': lambda: x is not None and x > val,
                'le': lambda: x is not None and x <= val,
                'lt': lambda: x is not None and x < val,
                'notnull': lambda: x is not None,
            }[op]()
            if not result:
                return False
        return True
    return [r for r in rows if accept(r)]


def main():
    rubric = json.loads((ROOT / 'rubric.json').read_text())
    subset = json.loads((ROOT / 'comparison_ids.json').read_text())
    assert len(f.CASES) == 64
    ids = [c[0] for c in f.CASES]
    assert len(ids) == len(set(ids))
    assert ids == [r['id'] for r in rubric['cases']]
    assert len(subset['ids']) == 30 and len(set(subset['ids'])) == 30
    assert set(subset['ids']) <= set(ids)
    assert set(f.EXPECTED_STATUSES) == set(ids) == set(f.FORBIDDEN)
    assert len(f.CUSTOMERS) == 10 and len(f.CONTRACTS) == 30
    assert len(f.SUPPLIERS) == 8 and len(f.FLOWS) == 7
    assert f.stamp('2026-09-30T23:59:59') < f.stamp('2026-10-01')
    assert f.stamp('2026-09-30T23:59:59') == f.stamp('2026-10-01') - 1000
    docs = f.documents()
    assert '分' in docs['components']['schemas']['Contract']['properties']['totalPrice']['description']
    assert f.documents() is not docs
    groups = {'contract': f.CONTRACTS, 'customer': f.CUSTOMERS, 'supplier': f.SUPPLIERS, 'flow': f.FLOWS}
    assert all(len(rs) == len({r['id'] for r in rs}) for rs in groups.values())
    for row in f.CONTRACTS:
        assert row['customerName'] == next(r['name'] for r in f.CUSTOMERS if r['id'] == row['customerId'])
        assert row['ownerUserName'] == next(r['nickname'] for r in f.USERS if r['id'] == row['ownerUserId'])
        assert row['totalPrice'] is None or isinstance(row['totalPrice'], int)
        assert row['auditStatus'] in (0, 10, 20)
    for cid, kind, prompt, checks in f.CASES:
        assert checks and prompt
        for pattern in checks + f.FORBIDDEN[cid]:
            re.compile(pattern)
    bycase = {cid: checks for cid, _, _, checks in f.CASES}
    # Guard regex boundaries and equivalents; compilation alone cannot detect escaping errors.
    assert re.search(bycase['v7-01'][0], '合同共有30份')
    assert re.search(bycase['v7-01'][0], '合同共有三十份')
    assert not re.search(bycase['v7-01'][0], '合同2030年下单')
    assert re.search(bycase['v7-20'][0], '合同金额0.01元')
    assert not re.search(bycase['v7-20'][0], '合同金额100元')
    assert re.search(bycase['v7-33'][0], '合计2,500.00元')
    assert re.search(bycase['v7-33'][0], '合计250,000分')
    assert not re.search(bycase['v7-33'][0], '合计1250000分')
    assert not re.search(bycase['v7-10'][0], '北辰门户二期续签')
    assert re.search(bycase['v7-43'][2], 'ui/docs/maintenance.md:3@synthetic-v7-1')
    checked_oracles = 0
    for case in rubric['cases']:
        assert case['required_facts'] and case['completeness'] and case['must_not'] and case['truth_basis']
        if 'oracle_scope' in case:
            scope = case['oracle_scope']
            rows = matching(groups[scope['entity']], scope['conditions'])
            assert [r['id'] for r in rows] == case['expected_record_ids'], case['id']
            if 'expected_count' in case:
                assert len(rows) == case['expected_count'], case['id']
            if 'expected_total_cents' in case:
                assert all(r['totalPrice'] is not None for r in rows), case['id']
                assert sum(r['totalPrice'] for r in rows) == case['expected_total_cents'], case['id']
                assert f"{case['expected_total_cents'] / 100:.2f}" == case['expected_total_yuan']
            checked_oracles += 1
        for evidence in case.get('source_evidence', []):
            assert evidence['release'] == f.RELEASE
            lines = f.SOURCE[evidence['path']].splitlines()
            start, end = evidence['lines']
            assert 1 <= start <= end <= len(lines)
        if 'expected_page_path' in case:
            assert f.PAGE_PATHS[case['id']] == case['expected_page_path']
    # Offline API contract: complete paging and actor-scoped scenes, independent of adapter/executor.
    client = f.Client()
    for base, rs in [('crm/contract', f.CONTRACTS), ('crm/customer', f.CUSTOMERS), ('erp/supplier', f.SUPPLIERS), ('bpm/process-instance/my', f.FLOWS)]:
        endpoint = 'bpm/process-instance/my-page' if base.endswith('/my') else base + '/page'
        received = []
        for page in range(1, 20):
            params = {'pageNo': page, 'pageSize': 3}
            if base.startswith('crm/'):
                params['sceneType'] = 3
            data = client.get(endpoint, **params)
            assert data['total'] == len(rs)
            received += data['list']
            if not data['list']:
                break
        assert [r['id'] for r in received] == [r['id'] for r in rs]
        # Result mutation must not mutate fixture truth.
        received[0]['name'] = 'changed only returned copy'
        assert rs[0]['name'] != received[0]['name']
        for r in rs:
            assert client.get(base + '/get', id=r['id'])['name'] == r['name']
    mine = client.get('crm/contract/page', sceneType=1)['list']
    others = client.get('crm/contract/page', sceneType=2)['list']
    assert len(mine) == 11 and len(others) == 19
    assert {r['id'] for r in mine}.isdisjoint({r['id'] for r in others})
    assert len(client.get('crm/contract/page', sceneType=3, customerId=2)['list']) == 2
    assert len(client.get('crm/contract/page', sceneType=3, auditStatus=20)['list']) == 17
    assert len(client.get('erp/supplier/page', status=1)['list']) == 3
    assert len(client.get('bpm/process-instance/my-page', name='出差申请')['list']) == 2
    denied = f.Client('restricted_contracts')
    assert 'crm:contract:query' not in denied.get('system/auth/get-permission-info')['permissions']
    assert denied.get('crm/customer/page', sceneType=3)['total'] == 10
    for operation in (lambda: denied.get('crm/contract/page', sceneType=3), lambda: client.post('crm/contract/update'), lambda: client.put('crm/contract/update'), lambda: client.delete('crm/contract/delete')):
        try:
            operation()
        except PermissionError:
            pass
        else:
            raise AssertionError('read/write permissions failed closed')
    try:
        client.get('crm/contract/page')
    except AssertionError:
        pass
    else:
        raise AssertionError('CRM scope required')
    assert not any(token in str(f.documents()) + str(f.SOURCE) for token in ('required_facts', 'expected_record_ids', 'oracle_scope', 'v7-01'))
    # Non-oracle mixed tasks: facts checked directly, never via production resolver.
    byid = {r['id']: r for r in f.CONTRACTS}
    assert max(r['totalPrice'] for r in f.CONTRACTS if r['totalPrice'] is not None) == 800000
    assert [r['id'] for r in f.CONTRACTS if r['totalPrice'] == 800000] == [113, 114]
    assert byid[103]['auditStatus'] == 10 and byid[104]['ownerUserName'] == '周试验'
    assert byid[109]['totalPrice'] == 99999 and byid[109]['auditStatus'] == 20
    assert sum(r['level'] == 1 for r in f.CUSTOMERS) == 5
    assert sum(r['status'] == 1 for r in f.SUPPLIERS) == 3
    assert '石桥服务组' in f.SOURCE['ui/docs/maintenance.md'] and '海桐界面组' in f.SOURCE['ui/docs/maintenance.md']
    outcomes = {k: sum(e['expected_outcome'] == k for e in rubric['cases']) for k in sorted({e['expected_outcome'] for e in rubric['cases']})}
    assert outcomes == {'clarification': 2, 'deliverable': 58, 'limitation': 3, 'partial_deliverable': 1}
    print(json.dumps({'cases': len(ids), 'comparison_cases': 30, 'structured_oracles_checked': checked_oracles, 'outcomes': outcomes, 'model_calls': 0, 'live_calls': 0, 'result': 'passed'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
