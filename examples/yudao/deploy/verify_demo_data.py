"""Read a synthetic baseline as its exploration actor; never invokes the assistant."""
from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

from demo_data_common import Records
from seed import Client

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'examples/yudao/src')]
from web_operation_agent_yudao.accounts import add_account_arguments, load_account  # noqa: E402


PAGES = {
    'crm/customer/page': 5000, 'crm/contact/page': 10000,
    'erp/product/page': 5000, 'erp/sale-order/page': 5000,
    'oa/meeting-room/page': 6, 'oa/supply-item/page': 20,
    'oa/vehicle/page': 6, 'oa/seal/page': 4,
    'oa/schedule/page': 20, 'oa/plan/page': 20, 'oa/work-report/page': 20,
    'hrm/recruit/channel/page': 4, 'hrm/recruit/post/page': 20,
    'hrm/recruit/candidate/page': 20, 'hrm/attendance/holiday/page': 20,
    'wms/warehouse/page': 6, 'wms/merchant/page': 6, 'wms/item-brand/page': 6,
    'wms/receipt-order/page': 20, 'wms/inventory-history/page': 20,
    'mes/md-workshop/page': 4, 'mes/md-workstation/page': 20,
    'mes/md/item/page': 20, 'mes/pro/process/page': 6,
    'mes/pro/route/page': 6, 'mes/pro/work-order/page': 20,
    'mes/tm/tool/page': 20, 'mes/qc/indicator/page': 20,
    'mes/qc/defect/page': 20, 'mes/qc/template/page': 6,
    'mes/cal/plan/page': 6, 'mes/cal/team/page': 6,
    'mes/wm/warehouse/page': 4, 'mes/wm/warehouse-location/page': 8,
    'mes/wm/warehouse-area/page': 20, 'pms/pm/project/page': 6,
    'pms/kb/library/page': 4, 'member/group/page': 5, 'member/tag/page': 6,
    'promotion/article/page': 20, 'hrm/salary/group/page': 1,
    'hrm/performance/result-template/page': 1,
}


def digest(rows):
    ordered = sorted(rows, key=lambda row: str(row.get('id', row.get('name', ''))))
    return hashlib.sha256(json.dumps(ordered, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def verify(base, account, output):
    c = Client(base, str(account.get('tenant', '1')))
    c.login(account['username'], account['password'])
    permission = c.get('system/auth/get-permission-info')
    permissions = permission.get('permissions', [])
    if any(value.endswith((':create', ':update', ':delete', ':approve')) for value in permissions):
        raise RuntimeError('Explorer role unexpectedly contains write permissions')
    def flatten(nodes):
        result = []
        for node in nodes:
            result.append(node)
            result.extend(flatten(node.get('children') or []))
        return result
    menus = flatten(permission['menus'])
    forbidden_roots = {'作者动态', 'Boot 开发文档', 'Cloud 开发文档', 'AI 大模型',
                 'IoT 物联网', '报表管理', 'CMS 内容管理', 'EAM 资产管理',
                 'MP 公众号管理', 'IM 即时通讯', '支付管理'}
    if (any(m['name'] in forbidden_roots for m in permission['menus'])
            or any(m['name'] in {'代码生成', '定时任务'} for m in menus)):
        raise RuntimeError('Excluded menu exposed to explorer')
    r = Records(c)
    checks = {}
    for path, minimum in PAGES.items():
        rows = r.rows(path)
        checks[path] = {'count': len(rows), 'minimum': minimum, 'sha256': digest(rows)}
        if len(rows) < minimum:
            raise RuntimeError('Insufficient visible records: ' + path)
    for path, params, minimum in [('product/spu/page', {'tabType': 0}, 20),
            ('wms/inventory/page', {'type': 'warehouse'}, 1)]:
        rows = r.rows(path, **params)
        if len(rows) < minimum:
            raise RuntimeError('Missing conditional list: ' + path)
        checks[path] = {'count': len(rows), 'minimum': minimum, 'params': params, 'sha256': digest(rows)}
    new_customers = {x['id'] for x in r.rows('crm/customer/page') if x['name'].startswith('合成演示·')}
    contacts = [x for x in r.rows('crm/contact/page') if x['name'].startswith('合成演示联系人')]
    links = Counter(x['customerId'] for x in contacts)
    if len(new_customers) != 5000 or len(contacts) != 9999 or set(links) != new_customers or not all(1 <= n <= 3 for n in links.values()):
        raise RuntimeError('Synthetic CRM relationship counts do not match the declared fixture')
    orders = [x for x in r.rows('erp/sale-order/page') if (x.get('remark') or '').startswith('synthetic-demo-v1-S')]
    if len(orders) != 5000 or len({x['remark'] for x in orders}) != 5000:
        raise RuntimeError('Missing or duplicate synthetic order keys')
    cents = lambda value: Decimal(str(value)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    samples = orders[:10] + orders[-10:]
    for order in samples:
        detail = c.get('erp/sale-order/get', id=order['id'])
        product = sum((Decimal(str(x['productPrice'])) * Decimal(str(x['count'])) for x in detail['items']), Decimal(0))
        tax = sum((Decimal(str(x['taxPrice'])) for x in detail['items']), Decimal(0))
        if cents(product) != cents(detail['totalProductPrice']) or cents(product + tax) != cents(detail['totalPrice']):
            raise RuntimeError('Order amounts do not match application line values')
    inventory = r.rows('wms/inventory/page', type='warehouse')
    if sum(Decimal(str(x['quantity'])) for x in inventory) != Decimal(2190):
        raise RuntimeError('WMS stock does not match the completed synthetic receipts')
    accounts = c.get('fms/config/account-set/list')
    if not accounts:
        raise RuntimeError('Explorer cannot access synthetic account set')
    for account_set in accounts:
        rows = r.rows('fms/voucher/page', accountSetId=account_set['id'])
        if len(rows) < 20:
            raise RuntimeError('Missing accounting vouchers')
        for voucher in rows:
            detail = c.get('fms/voucher/get', id=voucher['id'], accountSetId=account_set['id'])
            debit = sum((Decimal(str(x['debitAmount'])) for x in detail['entries']), Decimal(0))
            credit = sum((Decimal(str(x['creditAmount'])) for x in detail['entries']), Decimal(0))
            if debit != credit:
                raise RuntimeError('Accounting entries are not balanced')
        checks['fms/voucher/page'] = {'count': len(rows), 'params': {'accountSetId': account_set['id']},
                                    'sha256': digest(rows)}
    report = {'actor': permission['user']['id'], 'username': account['username'],
        'menu_count': len(menus), 'menu_names': sorted({m['name'] for m in menus}),
        'permissions': sorted(permissions), 'checks': checks, 'status': 'passed',
        'relationships': {'new_customers': len(new_customers), 'new_contacts': len(contacts),
            'new_orders': len(orders), 'orders_amount_checked': len(samples), 'wms_stock': 2190,
            'vouchers_balance_checked': 20},
        'scope': 'application API, same exploration actor; no model correctness claim'}
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    output.chmod(0o600)
    print(json.dumps({'status': report['status'], 'checked_collections': len(checks),
                      'counts': {k: v['count'] for k, v in checks.items()}}, ensure_ascii=False))
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--base', default='http://localhost:48163')
    add_account_arguments(ap)
    ap.add_argument('--output', required=True, type=Path)
    args = ap.parse_args()
    parsed = urlsplit(args.base)
    if parsed.username or parsed.password or parsed.scheme not in {'http', 'https'}:
        ap.error('invalid application origin')
    verify(args.base, load_account(args.account, args.accounts), args.output)


if __name__ == '__main__':
    main()
