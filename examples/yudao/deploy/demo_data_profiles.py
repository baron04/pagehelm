"""Synthetic business examples; IDs always come from the application's create/read APIs."""
from demo_data import MARK, stamp


def seed_groups(r, actor, size):
    def name(kind, i):
        return f'{MARK}·{kind}{i + 1:03d}'

    def items(route, kind, body, *, count=None, listing='page', key='name'):
        return [r.ensure(route, {'name': name(kind, i), **body(i)}, key=key, listing=listing)
                for i in range(count if count is not None else size)]

    def oa_assets():
        dept = r.rows('system/dept/list')[0]['id']
        items('oa/meeting-room', '会议室', lambda i: {'location': f'合成办公楼{i % 3 + 1}层',
            'type': 1, 'managerUserId': actor, 'status': 0, 'seatCount': 6 + i % 12,
            'equipments': [], 'allowBooking': True, 'needApproval': False, 'bookingScope': 0,
            'bookingUserIds': [], 'sort': i, 'fileUrls': [], 'remark': 'synthetic-demo-v1'}, count=6)
        items('oa/vehicle', '公务车辆', lambda i: {'no': f'合成-{i + 1:03d}', 'deptId': dept,
            'type': '商务车', 'seatCount': 5 + i % 3, 'barePrice': 100000 + i * 1000,
            'sort': i, 'status': 0, 'remark': '非真实车牌，仅合成记录'}, count=6)
        items('oa/supply-item', '办公用品', lambda i: {'deptId': dept, 'no': f'DEMO-SUP-{i}',
            'category': 1, 'manageType': 1, 'unit': '件', 'referencePrice': 5 + i,
            'stockQuantity': 20 + i, 'minStockQuantity': 5, 'sort': i, 'status': 0})
        items('oa/seal', '内部测试印章', lambda i: {'deptId': dept, 'keeperDeptId': dept,
            'keeperUserId': actor, 'type': 1, 'sort': i, 'status': 0,
            'remark': '仅合成记录，无实际印章'}, count=4)

    def oa_work():
        for i in range(size):
            r.ensure('oa/schedule', {'title': name('工作日程', i), 'type': 1 + i % 2,
                'priority': 1 + i % 3, 'description': '合成资料核对与内部协作',
                'startTime': stamp(-40 + i), 'endTime': stamp(-40 + i) + 3600000,
                'remind': False, 'participantUserIds': []}, key='title')
            r.ensure('oa/plan', {'title': name('工作计划', i), 'type': 1,
                'status': 1, 'content': '完成合成办公资料的分类整理、逐项核对及内部归档工作，不涉及真实客户。', 'startTime': stamp(-40 + i),
                'endTime': stamp(-39 + i), 'fileUrls': []}, key='title')
            r.ensure('oa/work-report', {'title': name('工作汇报', i), 'type': 1,
                'startTime': stamp(-40 + i), 'endTime': stamp(-39 + i),
                'summary': '已核对合成资料', 'plan': '继续完成内部整理',
                'problem': '无外部事项', 'workItems': [], 'planItems': [], 'fileUrls': []}, key='title')

    def hrm_recruit():
        channels = items('hrm/recruit/channel', '内部招聘渠道', lambda i: {'sort': i, 'remark': '仅合成渠道'}, count=4)
        posts = [r.ensure('hrm/recruit/post', {'postName': name('招聘岗位', i), 'jobNature': 1,
            'recruitNum': 1 + i % 3, 'minSalary': 8000 + i * 200, 'maxSalary': 12000 + i * 300,
            'salaryUnit': 2, 'reason': '合成项目人员规划', 'description': '测试岗位，无实际招聘'}, key='postName') for i in range(size)]
        items('hrm/recruit/candidate', '候选人', lambda i: {'mobile': f'138880{i:05d}',
            'sex': 1 + i % 2, 'education': 4, 'postId': posts[i], 'channelId': channels[i % 4],
            'email': f'candidate{i}@example.invalid', 'workTime': i % 8,
            'remark': '完全合成，无真实简历', 'resumeUrls': []})

    def hrm_holidays():
        for i in range(size):
            r.ensure('hrm/attendance/holiday', {'date': stamp(-90 + i * 3), 'type': 1 + i % 2}, key='date')

    def wms_base():
        items('wms/warehouse', '仓储分仓', lambda i: {'code': f'DEMO-WH-{i}', 'sort': i, 'remark': 'synthetic-demo-v1'}, count=6)
        items('wms/item-brand', '仓储品牌', lambda i: {'code': f'DEMO-WB-{i}'}, count=6)
        items('wms/merchant', '仓储货主', lambda i: {'code': f'DEMO-MER-{i}', 'type': 1 + i % 2,
            'contact': '合成联系人', 'email': f'merchant{i}@example.invalid', 'remark': 'synthetic-demo-v1'}, count=6)

    def mes_base():
        workshops = items('mes/md-workshop', '车间', lambda i: {'code': f'DEMO-MW-{i}',
            'area': 500 + i * 100, 'chargeUserId': actor, 'status': 0, 'remark': 'synthetic-demo-v1'}, count=4)
        processes = items('mes/pro/process', '工序', lambda i: {'code': f'DEMO-MP-{i}', 'status': 0,
            'attention': '按合成作业说明核对，不连接实体设备'}, count=6)
        items('mes/md-workstation', '工作站', lambda i: {'code': f'DEMO-MST-{i}',
            'workshopId': workshops[i % 4], 'processId': processes[i % 6], 'status': 0})
        items('mes/pro/route', '工艺路线', lambda i: {'code': f'DEMO-ROUTE-{i}',
            'description': '合成路线定义，未派发真实生产'}, count=6)
        items('mes/cal/team', '班组', lambda i: {'code': f'DEMO-TEAM-{i}', 'calendarType': 1}, count=6)

    def mes_material():
        unit = r.ensure('mes/md/unit-measure', {'name': MARK + '制造件', 'code': 'DEMO-PCS',
            'primaryFlag': True, 'changeRate': 1, 'status': 0, 'remark': 'synthetic-demo-v1'})
        categories = r.rows('mes/md/item-type/list')
        if not categories:
            category = r.ensure('mes/md/item-type', {'name': MARK + '制造物料', 'code': 'DEMO-MIT',
                'parentId': 0, 'sort': 0, 'status': 0, 'itemOrProduct': 'ITEM'}, listing='list')
        else:
            category = categories[0]['id']
        products = items('mes/md/item', '制造物料', lambda i: {'code': f'DEMO-MI-{i}',
            'unitMeasureId': unit, 'itemTypeId': category, 'specification': f'M{i % 8 + 1}',
            'safeStockFlag': True, 'minStock': 10, 'maxStock': 1000, 'highValue': False, 'batchFlag': True})
        for i in range(size):
            r.ensure('mes/pro/work-order', {'name': name('生产工单', i), 'code': f'DEMO-WO-{i}',
                'type': 1, 'orderSourceType': 1, 'productId': products[i], 'quantity': 20 + i,
                'requestDate': stamp(-20 + i % 20), 'parentId': 0, 'remark': 'synthetic-demo-v1'})

    def mes_tools():
        typ = r.ensure('mes/tm/tool-type', {'name': MARK + '工具类型', 'code': 'DEMO-TT',
            'codeFlag': True, 'maintenType': 1, 'maintenPeriod': 30})
        items('mes/tm/tool', '工具台账', lambda i: {'code': f'DEMO-T-{i}', 'toolTypeId': typ,
            'quantity': 10 + i, 'availableQuantity': 8 + i, 'status': 1, 'maintenType': 1})
        items('mes/qc/indicator', '检测项', lambda i: {'code': f'DEMO-QI-{i}', 'type': 1,
            'resultType': 1, 'tool': '合成量具'})
        items('mes/qc/defect', '缺陷分类', lambda i: {'code': f'DEMO-QD-{i}', 'type': 1, 'level': 1 + i % 3})

    def mes_warehouse():
        warehouses = items('mes/wm/warehouse', '制造仓库', lambda i: {'code': f'DEMO-MWH-{i}',
            'frozen': False, 'chargeUserId': actor, 'address': '合成工业园'}, count=4)
        locations = items('mes/wm/warehouse-location', '制造库区', lambda i: {
            'code': f'DEMO-LOC-{i}', 'warehouseId': warehouses[i % 4], 'frozen': False}, count=8)
        items('mes/wm/warehouse-area', '制造库位', lambda i: {'code': f'DEMO-AREA-{i}',
            'locationId': locations[i % 8], 'frozen': False, 'status': 0,
            'allowItemMixing': True, 'allowBatchMixing': True})

    def pms_work():
        projects = items('pms/pm/project', '协作项目', lambda i: {'type': 1, 'level': 1 + i % 3,
            'description': 'synthetic-demo-v1', 'openStatus': True, 'icon': 'ep:folder',
            'startTime': stamp(-60), 'endTime': stamp(-1), 'memberUserIds': [actor]}, count=6)
        for i, project in enumerate(projects):
            for j in range(3):
                r.ensure('pms/pm/work-item', {'projectId': project, 'type': 3,
                    'name': name('项目任务', i * 3 + j), 'priority': 1 + j, 'progress': 0,
                    'assigneeUserId': actor, 'memberUserIds': [actor],
                    'description': '内部合成任务，无外部通知', 'fileUrls': [], 'labelIds': []},
                    params={'projectId': project})

    def knowledge():
        items('pms/kb/library', '知识库', lambda i: {'description': '合成资料内部示例',
            'openStatus': True, 'adminUserIds': [actor], 'memberUserIds': []}, count=4)

    def member_base():
        items('member/group', '会员分组', lambda i: {'remark': 'synthetic-demo-v1', 'status': 0}, count=5)
        items('member/tag', '会员标签', lambda i: {}, count=6)
        for i in range(3):
            r.ensure('member/level', {'name': name('会员等级', i), 'level': i + 1,
                'experience': (i + 1) * 1000, 'discountPercent': 100 - i * 5, 'status': 0}, listing='list')

    return [('oa-assets', oa_assets), ('oa-work', oa_work), ('hrm-recruit', hrm_recruit),
            ('hrm-holidays', hrm_holidays), ('wms-base', wms_base), ('mes-base', mes_base),
            ('mes-material', mes_material), ('mes-tools', mes_tools), ('mes-warehouse', mes_warehouse),
            ('pms-work', pms_work), ('pms-knowledge', knowledge), ('member-base', member_base)]
