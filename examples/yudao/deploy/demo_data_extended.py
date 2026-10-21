"""Additional synthetic configuration, commerce catalog and balanced accounting examples."""
import json

from demo_data import MARK, stamp


def groups(r, actor, size):
    def mall():
        # A data URL avoids outbound image requests from synthetic catalog pages.
        pic = 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="64" height="64"%3E%3Crect width="64" height="64" fill="%23dbeafe"/%3E%3C/svg%3E'
        root = r.ensure('product/category', {'name': MARK + '商城商品', 'parentId': 0,
            'picUrl': pic, 'sort': 90, 'status': 0}, listing='list')
        category = r.ensure('product/category', {'name': MARK + '办公硬件', 'parentId': root,
            'picUrl': pic, 'sort': 90, 'status': 0}, listing='list')
        brand = r.ensure('product/brand', {'name': MARK + '商城品牌', 'picUrl': pic, 'sort': 90, 'status': 0})
        template = r.ensure('trade/delivery/express-template', {'name': MARK + '内部配送模板',
            'chargeMode': 1, 'sort': 90, 'frees': [], 'charges': [
                {'areaIds': [310115], 'startCount': 1, 'startPrice': 0, 'extraCount': 1, 'extraPrice': 0}]})
        for i in range(size):
            title = f'{MARK}·办公硬件{i + 1:03d}'
            r.ensure('product/spu', {'name': title, 'keyword': '合成商品',
                'introduction': '合成商品，无实际交易', 'description': '<p>仅用于内部数据查询</p>',
                'categoryId': category, 'brandId': brand, 'picUrl': pic, 'sliderPicUrls': [pic],
                'sort': i, 'specType': False, 'deliveryTypes': [1], 'deliveryTemplateId': template,
                'giveIntegral': 0, 'subCommissionType': False, 'virtualSalesCount': 0,
                'salesCount': 0, 'browseCount': 0, 'skus': [{'name': title, 'price': 1000 + i * 125,
                    'marketPrice': 2000 + i * 150, 'costPrice': 500 + i * 100,
                    'picUrl': pic, 'stock': 20 + i, 'properties': []}]}, params={'tabType': 0})
        article_cat = r.ensure('promotion/article-category', {'name': MARK + '资讯分类',
            'picUrl': pic, 'sort': 90, 'status': 0})
        for i in range(size):
            r.ensure('promotion/article', {'title': f'{MARK}·业务资讯{i + 1:03d}', 'author': '合成编辑',
                'categoryId': article_cat, 'spuId': 0, 'picUrl': pic, 'introduction': '合成资讯',
                'content': '<p>内部合成资料，用于列表与详情核对。</p>', 'sort': i, 'status': 0,
                'recommendBanner': False, 'recommendHot': False}, key='title')

    def hrm_config():
        tax = r.ensure('hrm/salary/tax-rule', {'name': MARK + '合成税务规则', 'type': 1,
            'taxEnabled': True, 'threshold': 5000, 'decimalScale': 2, 'cycleType': 1}, listing='list')
        dept = r.rows('system/dept/list')[0]['id']
        r.ensure('hrm/salary/group', {'name': MARK + '内部薪资组', 'taxRuleId': tax,
            'deptIds': [dept], 'employeeIds': []})
        r.ensure('hrm/performance/result-template', {'name': MARK + '绩效等级', 'levels': [
            {'name': '待改进', 'minScore': 0, 'maxScore': 59.99, 'coefficient': 0.8},
            {'name': '达标', 'minScore': 60, 'maxScore': 89.99, 'coefficient': 1},
            {'name': '优秀', 'minScore': 90, 'maxScore': 100, 'coefficient': 1.2}]})

    def finance():
        account = r.ensure('fms/config/account-set', {'companyCode': 'SYNTHETIC-DEMO',
            'companyName': MARK + '内部账套', 'industry': '合成服务业', 'location': '杭州',
            'legalRepresentative': '合成人员', 'contactName': '合成财务',
            'email': 'finance@example.invalid', 'address': '合成产业园'}, listing='list', key='companyCode')
        row = next(x for x in r.c.get('fms/config/account-set/list') if x['id'] == account)
        if not row.get('initialized'):
            r.c.put('fms/config/account-set/initialize', {'accountSetId': account, 'currencyCode': 'RMB',
                'startTime': stamp(-292), 'standard': 1, 'level': 4,
                'subjectCodeRule': '4-2-2-2', 'ledgerBalanceMode': 1})
        members = r.c.get('fms/config/account-user/list', accountSetId=account)
        if not any(m['userId'] == actor for m in members):
            keep = [{'userId': m['userId'], 'level': m['level']} for m in members if not m.get('founder')]
            r.c.put('fms/config/account-user/update', {'accountSetId': account,
                'members': keep + [{'userId': actor, 'level': 2}]})
        subjects = r.c.get('fms/config/subject/list', accountSetId=account)
        codes = {x['code']: x['id'] for x in subjects}
        word = r.ensure('fms/config/voucher-word', {'accountSetId': account, 'name': MARK + '记',
            'printTitle': '内部合成凭证', 'defaultStatus': False}, listing='list', params={'accountSetId': account})
        for i in range(size):
            amount = 100 + i * 12.5
            r.ensure('fms/voucher', {'accountSetId': account, 'voucherWordId': word,
                'voucherNumber': 9000 + i, 'voucherTime': stamp(-20 + i % 20), 'attachmentCount': 0,
                'entries': [{'digest': '合成内部资金调拨，无银行连接', 'subjectId': codes['1001'],
                             'debitAmount': amount, 'creditAmount': 0, 'auxiliaries': []},
                            {'digest': '合成内部资金调拨，无银行连接', 'subjectId': codes['1002'],
                             'debitAmount': 0, 'creditAmount': amount, 'auxiliaries': []}]},
                key='voucherNumber', params={'accountSetId': account})

    def bpm():
        manager = r.c.get('system/auth/get-permission-info')['user']['id']
        category = r.ensure('bpm/category', {'name': MARK + '内部流程', 'code': 'synthetic-demo',
            'status': 0, 'sort': 90}, key='code')
        form = r.ensure('bpm/form', {'name': MARK + '资料核对表', 'status': 0,
            'conf': json.dumps({'labelWidth': 100}), 'fields': [json.dumps({
                'type': 'input', 'field': 'subject', 'title': '核对事项', 'value': '合成资料核对'})],
            'remark': 'synthetic-demo-v1'})
        model = r.ensure('bpm/model', {'key': 'synthetic-demo-review', 'name': MARK + '内部核对',
            'category': 'synthetic-demo', 'type': 20, 'formType': 10, 'formId': form,
            'visible': True, 'managerUserIds': [manager], 'startUserIds': [manager, actor],
            'simpleModel': {'id': 'StartUserNode', 'type': 10, 'name': '发起人',
                'childNode': {'id': 'Activity_review', 'type': 11, 'name': '内部核对',
                    'candidateStrategy': 30, 'candidateParam': str(manager), 'approveType': 1, 'approveMethod': 4,
                    'rejectHandler': {'type': 1}, 'timeoutHandler': {'enable': False},
                    'assignEmptyHandler': {'type': 1}, 'assignStartUserHandlerType': 1,
                    'childNode': {'id': 'EndEvent', 'type': 1, 'name': '结束'}}}}, key='key', listing='list')
        row = next(x for x in r.c.get('bpm/model/list') if x['id'] == model)
        if not row.get('processDefinition'):
            r.c.post('bpm/model/deploy?id=' + str(model), None)

    return [('mall-catalog', mall), ('hrm-config', hrm_config), ('fms-finance', finance), ('bpm-model', bpm)]
