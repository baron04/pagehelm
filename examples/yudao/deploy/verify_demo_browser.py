"""Visible-page smoke check in a fresh browser profile, without model requests."""
import argparse
import json
from pathlib import Path
import sys

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'examples/yudao/src')]
from web_operation_agent_yudao.host.accounts import add_account_arguments, load_account  # noqa: E402


def verify(base, account, tenant_name, menu_file, output):
    menus = json.loads(menu_file.read_text())
    by_id = {m['id']: m for m in menus}
    components = ['crm/customer/index', 'erp/product/product/index', 'erp/sale/order/index',
                  'oa/supply/item/index', 'hrm/recruit/post/index', 'mall/product/spu/index']
    routes = []
    for component in components:
        leaf = next(m for m in menus if (m.get('component') or '').removesuffix('.vue') == component)
        parts, current = [], leaf
        while current:
            parts.append(current['path'].strip('/'))
            current = by_id.get(current.get('parentId'))
        routes.append('/' + '/'.join(reversed(parts)))
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        page.goto(base + '/login')
        page.locator('input[placeholder="请输入租户名称"]:visible').fill(tenant_name)
        page.locator('input[placeholder="请输入用户名"]:visible').fill(account['username'])
        page.locator('input[placeholder="请输入密码"]:visible').fill(account['password'])
        page.get_by_role('button', name='登录', exact=True).click()
        page.wait_for_url('**/index', timeout=60000)
        for i, route in enumerate(routes):
            page.goto(base + route)
            try:
                page.locator('.el-table__body-wrapper tr').first.wait_for(timeout=20000)
            except Exception:
                page.screenshot(path=str(output.parent / 'browser-failure.png'))
                output.with_suffix('.failure.json').write_text(json.dumps({
                    'expected_route': route, 'actual_url': page.url,
                    'visible_text': page.locator('body').inner_text()[:5000]}, ensure_ascii=False))
                raise
            count = page.locator('.el-table__body-wrapper tr').count()
            page.screenshot(path=str(output.parent / f'browser-{i + 1}.png'))
            results.append({'route': route, 'rows_visible': count, 'url_matches': page.url.split('?')[0] == base + route})
            if not count or not results[-1]['url_matches']:
                raise RuntimeError('Business page did not render expected rows: ' + route)
        browser.close()
    output.write_text(json.dumps({'status': 'passed', 'pages': results,
        'scope': 'six real pages in fresh exploration session; no assistant/model query'}, indent=2) + '\n')
    print(json.dumps(results))


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--base', default='http://localhost:48163')
    add_account_arguments(ap)
    ap.add_argument('--tenant-name', required=True)
    ap.add_argument('--menus', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    verify(args.base.rstrip('/'), load_account(args.account, args.accounts), args.tenant_name, args.menus, args.output)
