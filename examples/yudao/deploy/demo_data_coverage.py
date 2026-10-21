"""Additional local synthetic examples for retained business menus.

Definitions and relationships follow the native application; no external delivery,
payment, fake execution logs or direct database business writes are produced.
"""
from demo_data import MARK, stamp

PIC = 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="64" height="64"%3E%3Crect width="64" height="64" fill="%23dbeafe"/%3E%3C/svg%3E'


def groups(r, actor, size):
    def many(route, label, body, *, key='name', listing='page', count=5):
        return [r.ensure(route, {key: f'{MARK}覆盖·{label}{i + 1:03d}', **body(i)},
                         key=key, listing=listing) for i in range(count)]

    def mes_equipment():
        typ = r.ensure('mes/dv/machinery-type', {'name': MARK + '覆盖·机加工设备',
            'code': 'COV-DVT', 'parentId': 0, 'status': 0, 'sort': 90}, listing='list')
        workshop = next(x['id'] for x in r.rows('mes/md-workshop/page') if x['name'].startswith(MARK))
        many('mes/dv/machinery', '设备', lambda i: {'code': f'COV-DV-{i}', 'brand': '内部合成设备',
            'specification': f'M{i + 1}', 'machineryTypeId': typ, 'workshopId': workshop,
            'status': 0, 'remark': '未接入真实设备，仅合成台账'})
        many('mes/dv/subject', '设备点检项', lambda i: {'code': f'COV-SUB-{i}', 'type': 1 + i % 2,
            'content': '检查合成设备外观与润滑标记', 'standard': '外观完整，标记清晰', 'status': 0})
        many('mes/dv/check-plan', '设备检查方案', lambda i: {'code': f'COV-CHP-{i}',
            'type': 1 + i % 2, 'startDate': stamp(-30), 'endDate': stamp(-1),
            'cycleType': 1, 'cycleCount': 7, 'status': 0, 'remark': '未执行的内部合成方案'})

    def mes_rules():
        many('mes/md/auto-code-rule', '独立编码规则', lambda i: {'code': f'COV-RULE-{i}',
            'description': '独立示例，不替换已有业务编码规则', 'maxLength': 24,
            'padded': False, 'status': 1})
        many('mes/pro/andon-config', '内部安灯原因', lambda i: {'level': 1 + i % 3,
            'handlerUserId': actor, 'remark': '仅配置，不触发呼叫'}, key='reason')

    def bpm_reference():
        many('bpm/user-group', '内部核对组', lambda i: {'description': '合成成员分组',
            'userIds': [actor], 'status': 0})
        many('bpm/process-expression', '内部条件', lambda i: {'status': 1,
            'expression': '${false}'})

    def oa_template():
        many('oa/officialdoc-template', '内部公文模板', lambda i: {'authorityName': '合成资料核对组',
            'fontSize': 36, 'noPrefix': f'合成内字{i}', 'separatorType': 1,
            'status': 0, 'sort': i, 'remark': '仅模板，未发送公文'})

    def hrm_templates():
        many('hrm/salary/slip-template', '内部工资条模板', lambda i: {'hideEmpty': True, 'options': []}, listing='list')
        many('hrm/performance/assessment-template', '绩效核对模板', lambda i: {
            'illustrate': '只用于合成资料核验，不发放薪资', 'scoreCalculation': 1,
            'upperLimitType': 1, 'upperLimitScore': 100, 'dimensions': [{
                'name': '资料完整性', 'quotaType': 1, 'weight': 100,
                'allowEdit': False, 'quotas': [{'name': '核对完成情况', 'illustrate': '内部演示',
                    'standard': '按合成记录逐项核对', 'weight': 100, 'scoreType': 1}]}]})

    def pms_templates():
        many('pms/kb/library-template', '内部资料模板', lambda i: {'description': '合成资料归档',
            'status': 0, 'sort': i, 'documents': [{'title': '资料核对说明',
                'content': '<p>合成资料目录、核对范围与归档记录。</p>'}]})
        many('pms/pm/project-template', '内部项目模板', lambda i: {'description': '合成项目流程',
            'projectType': 1, 'status': 0, 'sort': i, 'itemTypes': [3], 'statuses': [{
                'code': 'todo', 'name': '待处理', 'workItemType': 3, 'statusType': 1,
                'defaultStatus': True, 'sort': 0, 'boardCode': 'todo'}], 'boards': [{
                'code': 'todo', 'name': '待处理', 'workItemType': 3, 'sort': 0, 'statusCodes': ['todo']}]})

    def catalog_config():
        many('promotion/banner', '内部展示条', lambda i: {'picUrl': PIC, 'status': 0,
            'url': '/pages/index/index', 'position': 1, 'sort': i,
            'memo': '无外部跳转的合成展示'}, key='title')
        many('promotion/seckill-config', '内部时段', lambda i: {'startTime': f'{8+i*2:02d}:00:00',
            'endTime': f'{9+i*2:02d}:00:00', 'sliderPicUrls': [PIC], 'status': 1})
        many('promotion/diy-page', '内部装修草稿', lambda i: {'remark': '仅页面草稿，未发布',
            'previewPicUrls': [PIC], 'property': '{"page":{"title":"合成资料"},"components":[]}'})
        many('promotion/diy-template', '内部装修模板', lambda i: {'remark': '未启用的合成模板',
            'previewPicUrls': [PIC], 'property': '{"page":{"title":"合成资料"},"components":[]}'})
        for i in range(7):
            r.ensure('member/sign-in/config', {'day': i + 1, 'point': (i + 1) * 2,
                'experience': i + 1, 'status': 0}, key='day', listing='list')

    return [('coverage-mes-equipment', mes_equipment), ('coverage-mes-rules', mes_rules),
            ('coverage-bpm', bpm_reference), ('coverage-oa-template', oa_template),
            ('coverage-hrm-templates', hrm_templates), ('coverage-pms-templates', pms_templates),
            ('coverage-catalog-config', catalog_config)]


def personal_records(r, actor, size=5):
    """Draft-only personal records: create does not submit native approval flows."""
    common = {'urgency': 1, 'reason': '合成资料核验所需，仅保存草稿', 'fileUrls': []}
    for route, kind, extra in [
        ('oa/leave-apply', '请假草稿', {'type': 1}),
        ('oa/overtime-apply', '加班草稿', {'type': 1}),
        ('oa/regular-apply', '转正草稿', {key: '合成记录，仅说明内部资料核对情况' for key in
            ['experience', 'understanding', 'growth', 'deficiency', 'improvement', 'suggestion']}),
        ('oa/resign-apply', '交接草稿', {'handoverUserId': 1, 'unfinishedWork': '合成资料整理',
            'hasPendingReimbursement': False}),
    ]:
        for i in range(size):
            body = {**common, **extra, 'title': f'{MARK}覆盖·{kind}{i + 1:03d}',
                    'startTime': stamp(-30 + i * 2), 'endTime': stamp(-30 + i * 2) + 3600000}
            r.ensure(route, body, key='title')
    rooms = [x for x in r.rows('oa/meeting-room/page') if x['name'].startswith(MARK)]
    seals = [x for x in r.rows('oa/seal/page') if x['name'].startswith(MARK)]
    vehicles = [x for x in r.rows('oa/vehicle/page') if x['name'].startswith(MARK)]
    supplies = [x for x in r.rows('oa/supply-item/page') if x['name'].startswith(MARK)]
    for i in range(size):
        title = f'{MARK}覆盖·会议草稿{i + 1:03d}'
        r.ensure('oa/meeting-room-booking', {'roomId': rooms[i % len(rooms)]['id'],
            'title': title, 'startTime': stamp(-25 + i), 'endTime': stamp(-25 + i) + 3600000,
            'moderatorUserId': actor, 'attendeeUserIds': [actor], 'reminderType': 1,
            'description': '合成会议预约草稿，不发送提醒', 'fileUrls': []}, key='title')
        r.ensure('oa/vehicle-apply', {'vehicleId': vehicles[i % len(vehicles)]['id'],
            'reason': f'{MARK}覆盖·用车草稿{i + 1:03d}', 'startTime': stamp(-25 + i),
            'endTime': stamp(-25 + i) + 3600000, 'startLocation': '合成办公区',
            'endLocation': '合成资料库', 'passenger': '合成核对员', 'fileUrls': []}, key='reason')
        r.ensure('oa/seal-apply', {'sealId': seals[i % len(seals)]['id'],
            'reason': f'{MARK}覆盖·用印草稿{i + 1:03d}', 'type': 2, 'mode': 1,
            'documentTitle': '合成内部材料', 'documentType': '内部材料', 'documentCount': 1,
            'expectedUseTime': stamp(-25 + i), 'urgent': False, 'fileUrls': []}, key='reason')
        r.ensure('oa/supply-apply', {'applyTime': stamp(-25 + i), 'useType': 1, 'pickupMethod': 1,
            'reason': f'{MARK}覆盖·用品草稿{i + 1:03d}', 'fileUrls': [],
            'items': [{'itemId': supplies[i % len(supplies)]['id'], 'applyQuantity': 1}]}, key='reason')
        r.ensure('oa/travel-apply', {'reason': f'{MARK}覆盖·差旅草稿{i + 1:03d}',
            'startTime': stamp(-25 + i), 'endTime': stamp(-24 + i), 'estimatedPrice': 100 + i * 20,
            'remark': '无实际出行，仅合成草稿', 'fileUrls': [], 'items': [{
                'departureAreaId': 330106, 'arrivalAreaId': 330108,
                'startTime': stamp(-25 + i), 'endTime': stamp(-24 + i), 'transportType': 1}]}, key='reason')
        r.ensure('oa/discussion', {'title': f'{MARK}覆盖·资料讨论{i + 1:03d}', 'type': 1,
            'content': '合成资料归档顺序与核对范围，内部演示，无外部通知。',
            'fileUrls': [], 'voteOptions': []}, key='title')


def manufacturing_documents(r, actor, size):
    """Header and line examples remain native drafts; no inventory execution is faked."""
    def deps(kind=1):
        first = lambda path: next(x['id'] for x in r.rows(path) if (x.get('name') or '').startswith(MARK))
        workorder = r.ensure('mes/pro/work-order', {'name': MARK + ('覆盖·备料工单' if kind == 1 else '覆盖·委外工单'),
            'code': 'COV-WO' if kind == 1 else 'COV-WO-OUT', 'type': kind, 'orderSourceType': 1,
            'productId': first('mes/md/item/page'), 'quantity': 500,
            'requestDate': stamp(-1), 'parentId': 0, 'remark': '合成内部备料，未完成生产'}, key='code')
        row = r.c.get('mes/pro/work-order/get', id=workorder)
        if row['status'] == 0:
            r.c.put('mes/pro/work-order/confirm', id=workorder)
        return {'workOrderId': workorder,
            'itemId': first('mes/md/item/page'), 'workstationId': first('mes/md-workstation/page'),
            'vendorId': next(x['id'] for x in r.rows('mes/md-vendor/page') if x['status'] == 0),
            'clientId': next(x['id'] for x in r.rows('mes/md-client/page') if x['status'] == 0)}
    actions = []
    definitions = [
        ('arrival-notice', '到货通知草稿', lambda d,i: {'vendorId':d['vendorId'],'arrivalDate':stamp(-20+i)}),
        ('sales-notice', '发货通知草稿', lambda d,i: {'clientId':d['clientId'],'salesDate':stamp(-20+i)}),
        ('misc-receipt', '其他入库草稿', lambda d,i: {'type':1,'receiptDate':stamp(-20+i)}),
        ('misc-issue', '其他出库草稿', lambda d,i: {'type':1,'issueDate':stamp(-20+i)}),
        ('item-receipt', '物料入库草稿', lambda d,i: {'vendorId':d['vendorId'],'receiptDate':stamp(-20+i)}),
        ('product-receipt', '产品入库草稿', lambda d,i: {'workOrderId':d['workOrderId'],'receiptDate':stamp(-20+i)}),
        ('product-issue', '生产领料草稿', lambda d,i: {'workOrderId':d['workOrderId'],'workstationId':d['workstationId'],'requiredTime':stamp(-20+i)}),
        ('return-issue', '生产退料草稿', lambda d,i: {'workOrderId':d['workOrderId'],'workstationId':d['workstationId'],'type':1,'returnDate':stamp(-20+i)}),
        ('outsource-issue', '委外发料草稿', lambda d,i: {'workOrderId':d['workOrderId'],'vendorId':d['vendorId'],'issueDate':stamp(-20+i)}),
        ('outsource-receipt', '委外入库草稿', lambda d,i: {'workOrderId':d['workOrderId'],'vendorId':d['vendorId'],'receiptDate':stamp(-20+i)}),
        ('return-sales', '销售退货草稿', lambda d,i: {'clientId':d['clientId'],'returnDate':stamp(-20+i),'returnReason':'合成内部退货草稿'}),
        ('return-vendor', '供应商退货草稿', lambda d,i: {'vendorId':d['vendorId'],'returnDate':stamp(-20+i),'returnReason':'合成内部退货草稿'}),
        ('product-sales', '销售出库草稿', lambda d,i: {'clientId':d['clientId'],'salesDate':stamp(-20+i)}),
        ('transfer', '转移草稿', lambda d,i: {'type':1,'deliveryFlag':False,'transferDate':stamp(-20+i)}),
        ('stocktaking-plan', '库存盘点计划', lambda d,i: {'type':1,'blindFlag':False,'frozen':False,'startTime':stamp(-20+i),'endTime':stamp(-19+i)}),
    ]
    for route,label,body in definitions:
        def action(route=route,label=label,body=body):
            d=deps(2 if route == 'outsource-issue' else 1)
            for i in range(5):
                r.ensure('mes/wm/'+route, {'name':f'{MARK}覆盖·{label}{i+1:03d}',
                    'code':f'COV-{route}-{i+1:03d}','remark':'synthetic-coverage-v2; 未执行库存变更',**body(d,i)},key='code')
        actions.append(('coverage-mes-'+route,action))
    def inspection():
        d=deps()
        template = r.ensure('mes/qc/template', {'name': MARK + '覆盖·四类检验方案',
            'code': 'COV-QCT', 'types': [1, 2, 3, 4], 'status': 0, 'remark': '内部合成检验定义'}, key='code')
        r.ensure('mes/qc/template/item', {'templateId': template, 'itemId': d['itemId'],
            'quantityCheck': 10, 'quantityUnqualified': 0, 'criticalRate': 0,
            'majorRate': 0, 'minorRate': 0}, key='itemId', params={'templateId': template})
        indicator = next(x['id'] for x in r.rows('mes/qc/indicator/page') if x['name'].startswith(MARK))
        r.ensure('mes/qc/template/indicator', {'templateId': template, 'indicatorId': indicator,
            'checkMethod': '合成尺寸核对', 'standardValue': 10, 'thresholdMin': 9.5,
            'thresholdMax': 10.5}, key='indicatorId', params={'templateId': template})
        for i in range(5):
            for kind,body in [
                ('iqc',{'vendorId':d['vendorId'],'receivedQuantity':100+i,'receiveDate':stamp(-20+i)}),
                ('oqc',{'clientId':d['clientId'],'outQuantity':100+i,'outDate':stamp(-20+i),'checkQuantity':10}),
                ('rqc',{'type':1,'checkQuantity':10}),
                ('ipqc',{'type':1,'workOrderId':d['workOrderId'],'workstationId':d['workstationId'],
                         'checkQuantity':10,'laborScrapQuantity':0,'materialScrapQuantity':0,'otherScrapQuantity':0}),
            ]:
                r.ensure('mes/qc/'+kind, {'name':f'{MARK}覆盖·检验草稿{kind}{i+1:03d}',
                    'code':f'COV-QC-{kind}-{i}','itemId':d['itemId'],'qualifiedQuantity':100+i if kind == 'iqc' else 10,
                    'unqualifiedQuantity':0,'inspectorUserId':actor,'inspectDate':stamp(-20+i),
                    'remark':'合成检验草稿，未执行真实设备检验',**body},key='code')
    actions.append(('coverage-mes-inspections',inspection))
    def device_records():
        machine=next(x['id'] for x in r.rows('mes/dv/machinery/page') if x['name'].startswith(MARK))
        for i in range(5):
            for kind,key in [('check-record','checkTime'),('mainten-record','maintenTime')]:
                r.ensure('mes/dv/'+kind,{'machineryId':machine,'userId':actor,
                    key:stamp(-20+i),'remark':f'synthetic-coverage-v2-{kind}-{i}'},key='remark')
            r.ensure('mes/dv/repair',{'code':f'COV-REPAIR-{i}','name':f'{MARK}覆盖·检修草稿{i}',
                'machineryId':machine,'requireDate':stamp(-20+i),'remark':'合成台账草稿，未完成实际检修'},key='code')
    actions.append(('coverage-mes-device-records',device_records))
    def production():
        rule = r.ensure('mes/md/auto-code-rule', {'name': MARK + '覆盖·生产任务编码',
            'code': 'PRO_TASK_CODE', 'maxLength': 24, 'padded': False, 'status': 0,
            'remark': '补齐此前缺失的生产任务编码定义'}, key='code')
        r.ensure('mes/md/auto-code-part', {'ruleId': rule, 'sort': 1, 'type': 3,
            'length': 7, 'fixCharacter': 'COVTASK'}, key='sort',
            listing='list-by-rule-id', params={'ruleId': rule})
        r.ensure('mes/md/auto-code-part', {'ruleId': rule, 'sort': 2, 'type': 4,
            'length': 6, 'serialStartNo': 1, 'serialStep': 1, 'cycleFlag': False}, key='sort',
            listing='list-by-rule-id', params={'ruleId': rule})
        d=deps();process=r.rows('mes/pro/process/page')[0]['id'];route=r.rows('mes/pro/route/page')[0]['id']
        for i in range(5):
            r.ensure('mes/pro/card',{'code':f'COV-CARD-{i}','workOrderId':d['workOrderId'],
                'itemId':d['itemId'],'transferedQuantity':10+i,'remark':'合成流转卡，未执行生产'},key='code')
            r.ensure('mes/pro/task',{'workOrderId':d['workOrderId'],'workstationId':d['workstationId'],
                'processId':process,'routeId':route,'itemId':d['itemId'],'quantity':10+i,
                'startTime':stamp(-20+i),'endTime':stamp(-20+i)+3600000,'duration':60,
                'colorCode':'#409eff','remark':f'synthetic-coverage-v2-task-{i}'},key='remark')
    actions.append(('coverage-mes-production',production))
    return actions


def crm_personal(r, actor, size):
    customers = [x for x in r.rows('crm/customer/page')
                 if x.get('ownerUserId') == actor and x['name'].startswith(MARK)]
    if not customers:
        raise RuntimeError('Requires exploration-owned synthetic customers')
    group = r.rows('crm/business-status/page')[0]['id']
    product = next(x for x in r.rows('crm/product/page') if x['status'] == 1)
    for i in range(size):
        customer = customers[i % len(customers)]['id']
        contact = r.ensure('crm/contact', {'name': f'{MARK}覆盖·联系员{i + 1:03d}',
            'customerId': customer, 'ownerUserId': actor, 'master': False,
            'post': '内部资料核对', 'email': f'coverage-contact-{i}@example.invalid'})
        r.ensure('crm/clue', {'name': f'{MARK}覆盖·线索{i + 1:03d}',
            'ownerUserId': actor, 'source': 1 + i % 3, 'industryId': 1 + i % 6, 'level': 1 + i % 3,
            'remark': 'synthetic-coverage-v2; 无外部联络'})
        price = product['price']
        business = r.ensure('crm/business', {'name': f'{MARK}覆盖·资料项目{i + 1:03d}',
            'ownerUserId': actor, 'customerId': customer, 'contactId': contact, 'statusTypeId': group,
            'dealTime': stamp(-20 + i), 'discountPercent': 0, 'products': [{
                'productId': product['id'], 'productPrice': price,
                'businessPrice': price, 'count': i + 1}]})
        r.ensure('crm/contract', {'name': f'{MARK}覆盖·内部草稿合同{i + 1:03d}',
            'ownerUserId': actor, 'customerId': customer, 'businessId': business, 'orderDate': stamp(-20 + i),
            'startTime': stamp(-20 + i), 'endTime': stamp(30 + i), 'discountPercent': 0,
            'totalPrice': price * (i + 1), 'signContactId': contact, 'signUserId': actor,
            'products': [{'productId': product['id'], 'productPrice': price,
                'contractPrice': price, 'count': i + 1}]})


def warehouse_documents(r, actor, size):
    def documents():
        warehouses = [x for x in r.rows('wms/warehouse/page') if x['name'].startswith(MARK)]
        merchants = [x for x in r.rows('wms/merchant/page') if x['type'] in {1, 3}]
        stock = r.rows('wms/inventory/page', type='warehouse')[0]
        source = stock['warehouseId']; target = next(x['id'] for x in warehouses if x['id'] != source)
        for i in range(5):
            detail = {'skuId': stock['skuId'], 'quantity': 1 + i, 'price': 10, 'totalPrice': (1 + i) * 10}
            r.ensure('wms/shipment-order', {'no': f'COV-OUT-{i + 1:03d}', 'type': 201,
                'orderTime': stamp(-15 + i), 'warehouseId': source, 'merchantId': merchants[0]['id'],
                'remark': 'synthetic-coverage-v2; 出库草稿未执行', 'details': [detail]}, key='no')
            r.ensure('wms/movement-order', {'no': f'COV-MOVE-{i + 1:03d}',
                'orderTime': stamp(-15 + i), 'sourceWarehouseId': source, 'targetWarehouseId': target,
                'remark': 'synthetic-coverage-v2; 移库草稿未执行', 'details': [detail]}, key='no')
            r.ensure('wms/check-order', {'no': f'COV-CHECK-{i + 1:03d}', 'orderTime': stamp(-15 + i),
                'warehouseId': source, 'remark': 'synthetic-coverage-v2; 盘点草稿未执行',
                'details': [{'skuId': stock['skuId'], 'quantity': stock['quantity'],
                    'checkQuantity': stock['quantity'], 'price': 10}]}, key='no')
    return [('coverage-wms-documents', documents)]
