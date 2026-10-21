"""Prepare an isolated synthetic demo through application APIs, never through a model.

Explicit --apply and an operator-created database backup are mandatory. Mutations
are journaled without request bodies. Failed/uncertain dispatches are not retried.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import gzip
import json
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit

from demo_data_common import DemoClient, Records, menu_scope
from seed import ApiError

MARK = '合成演示'
ANCHOR = datetime(2026, 10, 20, 10, tzinfo=timezone(timedelta(hours=8)))


def date(i=0):
    return (ANCHOR + timedelta(days=i)).strftime('%Y-%m-%d %H:%M:%S')


def stamp(i=0):
    return int((ANCHOR + timedelta(days=i)).timestamp() * 1000)


def private_json(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.chmod(path, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(value, out, ensure_ascii=False, indent=2)
        out.write('\n')


def prepare_menus(r, state):
    menus = r.c.get('system/menu/list')
    scope = menu_scope(menus)
    if not (state / 'menus-original.json').exists():
        private_json(state / 'menus-original.json', menus)
        private_json(state / 'menu-scope.json', scope)
    for ident in scope['remove']:
        r.c.call('DELETE', 'system/menu/delete', query={'id': ident})
    for ident in scope['disable']:
        menu = next(m for m in menus if m['id'] == ident)
        if menu['status'] == 0:
            r.c.put('system/menu/update', {**menu, 'status': 1})
    role = r.ensure('system/role', {'name': '合成数据只读探索', 'code': 'synthetic_demo_reader',
                    'sort': 90, 'status': 0, 'remark': 'Read-only synthetic demo, no write grants'},
                    key='code')
    r.c.post('system/permission/assign-role-menu', {'roleId': role, 'menuIds': scope['explorer_grants']})
    r.c.post('system/permission/assign-role-data-scope', {'roleId': role, 'dataScope': 1, 'dataScopeDeptIds': []})
    credential = state.parent / 'demo-account.json'
    if credential.exists():
        account = json.loads(credential.read_text())
    else:
        account = {'username': 'demoexplorer', 'password': secrets.token_urlsafe(12), 'tenant': '1'}
        private_json(credential, account)
    actor = r.ensure('system/user', {'username': account['username'], 'password': account['password'],
                     'nickname': '合成演示探索员', 'remark': 'Synthetic read-only exploration account'}, key='username')
    r.c.post('system/permission/assign-user-role', {'userId': actor, 'roleIds': [role]})
    private_json(state / 'identity.json', {'actor': actor, 'role': role})
    current = r.c.get('system/menu/list')
    assert not any(m['id'] in scope['remove'] for m in current)
    assert all(m['status'] == 1 for m in current if m['id'] in scope['disable'])
    print(json.dumps({'removed': len(scope['remove']), 'disabled': len(scope['disable']),
                      'explorer_grants': len(scope['explorer_grants']), 'actor': actor}), flush=True)


def broad(r, actor, size, outcomes, selected=None):
    from demo_data_profiles import seed_groups
    from demo_data_extended import groups
    from demo_data_flows import groups as flow_groups
    from demo_data_coverage import groups as coverage_groups, manufacturing_documents, warehouse_documents
    actions = seed_groups(r, actor, size) + groups(r, actor, size) + flow_groups(r, actor, size) + coverage_groups(r, actor, size) + manufacturing_documents(r, actor, size) + warehouse_documents(r, actor, size)
    if selected and selected - {name for name, _ in actions}:
        raise ValueError('Unknown group selection')
    for name, action in actions:
        if selected and name not in selected:
            continue
        try:
            action()
            outcomes.append({'group': name, 'status': 'complete'})
        except ApiError as exc:
            if r.c.uncertain:
                raise
            # A definite application rejection stops this group. Independent groups
            # may continue; no rejected operation is retried with altered arguments.
            outcomes.append({'group': name, 'status': 'rejected', 'code': exc.code, 'diagnostic': str(exc)})
        print(json.dumps(outcomes[-1], ensure_ascii=False), flush=True)


def large(r, actor, size):
    cities = ('杭州', '苏州', '深圳', '成都', '武汉', '青岛')
    customers = []
    for i in range(size):
        key = f'{MARK}·{cities[i % 6]}客户{i + 1:05d}'
        cid = r.ensure('crm/customer', {'name': key, 'ownerUserId': actor, 'industryId': 1 + i % 6,
            'level': 1 + i % 3, 'source': 1 + i % 6, 'detailAddress': cities[i % 6] + '合成园区',
            'remark': 'synthetic-demo-v1'})
        customers.append(cid)
        # Vary one/ two/ three contacts; 5,000 customers produce 9,999 contacts.
        for j in range(1 + i % 3):
            r.ensure('crm/contact', {'name': f'{MARK}联系人{i + 1:05d}-{j + 1}',
                'customerId': cid, 'ownerUserId': actor, 'master': j == 0,
                'post': ('采购', '技术', '财务')[j], 'email': f'demo{i}-{j}@example.invalid'})
        if (i + 1) in {100, 500, 1000, 5000}:
            print(json.dumps({'checkpoint': 'crm', 'customers': i + 1}), flush=True)
    unit = r.ensure('erp/product-unit', {'name': MARK + '件', 'status': 0})
    category = r.ensure('erp/product-category', {'name': MARK + '规模物料', 'parentId': 0,
        'code': 'DEMO-CAT', 'sort': 90, 'status': 0}, listing='list')
    products = [r.ensure('erp/product', {'name': f'{MARK}·工业配件{i + 1:05d}',
        'barCode': f'DEMO-EP-{i + 1:05d}', 'categoryId': category, 'unitId': unit,
        'status': 0, 'purchasePrice': round(5 + i % 100 * 1.25, 2),
        'salePrice': round(8.5 + i % 100 * 2.15, 2), 'minPrice': 1}, key='barCode') for i in range(size)]
    partner = r.ensure('erp/customer', {'name': MARK + '规模测试客户', 'status': 0, 'sort': 90})
    account = r.ensure('erp/account', {'name': MARK + '内部结算', 'no': 'SYNTHETIC-NONBANK-DEMO',
        'status': 0, 'sort': 90})
    for i in range(size):
        indices = [(i + j) % size for j in range(2 + i % 2)]
        r.ensure('erp/sale-order', {'customerId': partner, 'accountId': account, 'saleUserId': actor,
            'orderTime': stamp(-180 + i % 180), 'discountPercent': 0,
            'remark': f'synthetic-demo-v1-S{i + 1:05d}',
            'items': [{'productId': products[k], 'productUnitId': unit,
                       'productPrice': round(8.5 + k % 100 * 2.15, 2),
                       'count': 1 + (i + j) % 12, 'taxPercent': 13} for j, k in enumerate(indices)]}, key='remark')
        if (i + 1) in {100, 500, 1000, 5000}:
            print(json.dumps({'checkpoint': 'erp', 'products': len(products), 'orders': i + 1}), flush=True)


def personal(r, state, actor, size, *, coverage=False):
    """Seed actor-private lists, then revoke the narrowly scoped temporary setup role."""
    from demo_data_profiles import seed_groups
    identity = json.loads((state / 'identity.json').read_text())
    menus = r.c.get('system/menu/list')
    permissions = {'oa:schedule:create', 'oa:plan:create', 'oa:work-report:create'}
    if coverage:
        permissions = {'oa:' + name + ':create' for name in ['leave-apply', 'overtime-apply',
            'regular-apply', 'resign-apply', 'meeting-room-booking', 'vehicle-apply',
            'seal-apply', 'supply-apply', 'discussion']} | {'oa:travel-apply:save', 'crm:clue:create', 'crm:contact:create', 'crm:business:create', 'crm:contract:create'}
    grants = [m['id'] for m in menus if m.get('permission') in permissions]
    if len(grants) != len(permissions):
        raise RuntimeError('Missing exact personal-data setup permissions')
    setup = r.ensure('system/role', {'name': '合成数据临时准备', 'code': 'synthetic_demo_setup',
        'sort': 99, 'status': 0, 'remark': 'Temporary local seeding only; no standing user grants'}, key='code')
    r.c.post('system/permission/assign-role-menu', {'roleId': setup, 'menuIds': grants})
    try:
        r.c.post('system/permission/assign-user-role', {'userId': actor, 'roleIds': [identity['role'], setup]})
        c = DemoClient(r.c.base, '1', state / ('coverage-personal-actor-writes.jsonl' if coverage else 'personal-actor-writes.jsonl'))
        account = json.loads((state.parent / 'demo-account.json').read_text())
        c.login(account['username'], account['password'])
        own = Records(c)
        if coverage:
            from demo_data_coverage import personal_records, crm_personal
            personal_records(own, actor, size)
            crm_personal(own, actor, size)
        else:
            action = next(action for name, action in seed_groups(own, actor, size) if name == 'oa-work')
            action()
        r.created.update(own.created)
    finally:
        r.c.post('system/permission/assign-user-role', {'userId': actor, 'roleIds': [identity['role']]})
        r.c.post('system/permission/assign-role-menu', {'roleId': setup, 'menuIds': []})


def ownership(r, actor):
    """Use the application's transfer flow; creation ignores the requested owner."""
    customers = [x for x in r.rows('crm/customer/page') if x['name'].startswith(MARK + '·')]
    if len(customers) != 5000:
        raise RuntimeError('Requires exactly the declared 5,000 new synthetic customers')
    transferred = 0
    for row in customers:
        if row.get('ownerUserId') == actor:
            continue
        r.c.put('crm/customer/transfer', {'id': row['id'], 'newOwnerUserId': actor,
            'oldOwnerPermissionLevel': 1, 'toBizTypes': [3]})
        transferred += 1
        if transferred % 500 == 0:
            print(json.dumps({'customers_and_contacts_transferred': transferred}), flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('stage', choices=['menus', 'broad', 'large', 'personal', 'coverage-personal', 'ownership'])
    ap.add_argument('--base', default='http://localhost:48163')
    ap.add_argument('--account', type=Path, required=True)
    ap.add_argument('--backup', type=Path, required=True)
    ap.add_argument('--state', type=Path, required=True)
    ap.add_argument('--size', type=int, default=20)
    ap.add_argument('--groups', help='Explicit comma-separated independent broad groups')
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    parsed = urlsplit(args.base)
    if not args.apply or parsed.scheme != 'http' or parsed.hostname not in {'localhost', '127.0.0.1'} or parsed.username or parsed.password or parsed.path not in {'', '/'} or parsed.query or parsed.fragment:
        ap.error('requires --apply and a loopback application URL')
    if not 1 <= args.size <= (5000 if args.stage == 'large' else 50):
        ap.error('size out of bounds')
    with gzip.open(args.backup, 'rb') as source:
        if not source.read(100).startswith(b'-- MySQL dump'):
            ap.error('requires an existing MySQL backup')
    args.state.mkdir(parents=True, exist_ok=True)
    args.state.chmod(0o700)
    c = DemoClient(args.base, '1', args.state / (args.stage + '-writes.jsonl'))
    account = json.loads(args.account.read_text())
    c.login(account['username'], account['password'])
    r, outcomes = Records(c), []
    if args.stage == 'menus':
        prepare_menus(r, args.state)
    else:
        actor = json.loads((args.state / 'identity.json').read_text())['actor']
        if args.stage == 'ownership':
            ownership(r, actor)
        elif args.stage in {'personal', 'coverage-personal'}:
            personal(r, args.state, actor, args.size, coverage=args.stage == 'coverage-personal')
        elif args.stage == 'broad':
            broad(r, actor, args.size, outcomes, set(args.groups.split(',')) if args.groups else None)
        else:
            large(r, actor, args.size)
    private_json(args.state / f'{args.stage}-result.json', {'created': r.created, 'outcomes': outcomes})
    if any(x['status'] != 'complete' for x in outcomes):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
