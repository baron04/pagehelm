"""Small application-owned flows producing related operational records."""
from demo_data import MARK, stamp


def groups(r, actor, size):
    def wms_orders():
        warehouses = [x for x in r.rows('wms/warehouse/page') if x['name'].startswith(MARK)]
        merchants = [x for x in r.rows('wms/merchant/page') if x['name'].startswith(MARK) and x['type'] in {2, 3}]
        skus = r.rows('wms/item-sku/page')
        if not skus:
            raise RuntimeError('Requires real warehouse SKU records')
        for i in range(size):
            order = r.ensure('wms/receipt-order', {'no': f'DEMO-IN-{i + 1:04d}', 'type': 101,
                'orderTime': stamp(-40 + i), 'warehouseId': warehouses[i % len(warehouses)]['id'],
                'merchantId': merchants[i % len(merchants)]['id'], 'remark': 'synthetic-demo-v1',
                'details': [{'skuId': skus[i % len(skus)]['id'], 'quantity': 100 + i,
                    'price': 10 + i, 'totalPrice': (100 + i) * (10 + i)}]}, key='no')
            row = r.c.get('wms/receipt-order/get', id=order)
            if row.get('status') == 0:
                r.c.put('wms/receipt-order/complete', id=order)

    def mes_operations():
        for i in range(6):
            r.ensure('mes/qc/template', {'name': f'{MARK}检验方案{i + 1}', 'code': f'DEMO-QCT-{i}',
                'types': [1, 3], 'status': 0, 'remark': '内部合成方案'})
            r.ensure('mes/cal/plan', {'name': f'{MARK}排班计划{i + 1}', 'code': f'DEMO-CP-{i}',
                'calendarType': 1, 'startDate': stamp(-90 + i * 7), 'endDate': stamp(-84 + i * 7),
                'shiftType': 1, 'shiftMethod': 1, 'shiftCount': 7, 'status': 0,
                'remark': '内部合成配置，未执行排班'})

    def hrm_clock():
        users = r.rows('system/user/page')
        person = next(x for x in users if x['id'] == actor)
        dept = r.rows('system/dept/list')[0]['id']
        employee = r.ensure('hrm/employee', {'name': person['nickname'], 'userId': actor,
            'deptId': dept, 'jobNumber': 'DEMO-EXPLORER', 'sex': 1, 'entryStatus': 1,
            'status': 1, 'type': 1, 'entryTime': stamp(-365), 'probation': 0,
            'postName': '合成数据核对', 'workCity': '杭州', 'mobile': '13988999999',
            'email': 'explorer@example.invalid'}, key='jobNumber')
        r.ensure('hrm/attendance/group', {'name': MARK + '内部考勤组', 'openWifiCard': True,
            'openPointCard': False, 'rest': False, 'specialDates': [], 'deptIds': [],
            'employeeIds': [employee], 'points': [],
            'wifis': [{'ssid': 'SYNTHETIC-NONNETWORK', 'mac': '02:00:00:00:00:01'}],
            'shifts': [{'weeks': [1, 2, 3, 4, 5, 6, 7], 'startTime': '09:00', 'endTime': '18:00',
                        'clockInStartTime': '08:00', 'clockInEndTime': '10:00',
                        'clockOutStartTime': '17:00', 'clockOutEndTime': '20:00',
                        'restStartTime': '12:00', 'restEndTime': '13:00', 'excludeRestTime': True}],
            'deductRule': {'lateMethod': 1, 'lateDeductMoney': 0, 'earlyMethod': 1,
                'earlyDeductMoney': 0, 'absenteeismMethod': 1, 'absenteeismDeductMoney': 0,
                'misscardMethod': 1, 'misscardDeductMoney': 0}})
        for i in range(size):
            r.ensure('hrm/attendance/clock', {'employeeId': employee, 'type': 1,
                'clockTime': stamp(-40 + i) - 3600000 + (i % 3) * 300000,
                'attendanceTime': stamp(-40 + i) - 3600000,
                'address': '合成办公园区', 'remark': f'synthetic-demo-clock-{i + 1:03d}'}, key='remark')

    return [('wms-receipts', wms_orders), ('mes-operations', mes_operations), ('hrm-clock', hrm_clock)]
