"""yudao goal data over a recorded-shape API double: schema, reads and resolution."""
from __future__ import annotations

import unittest
from copy import deepcopy
from unittest.mock import patch

from web_operation_agent.goal.data import EVERYTHING, Compare, DataRefused, all_of
from web_operation_agent.goal.resolve.service import DataGoalPort
from web_operation_agent.goal.spec import GoalSpec

from web_operation_agent_yudao.goal import YudaoGoalData
from web_operation_agent_yudao.ui_source import ListView, UiSource


def _prop(kind, description, fmt=None):
    out = {"type": kind, "description": description}
    if fmt:
        out["format"] = fmt
    return out


def _page(tag, item, params):
    return {"get": {"tags": [tag], "parameters": [
        {"in": "query", "name": n, "schema": {"type": t}} for n, t in params],
        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": f"#/components/schemas/R{item}"}}}}}}}


def _schemas(item, props):
    return {f"R{item}": {"properties": {"data": {"$ref": f"#/components/schemas/P{item}"}}},
            f"P{item}": {"properties": {"list": {"items": {"$ref": f"#/components/schemas/{item}"}}}},
            item: {"properties": props}}


DOCS = {
    "paths": {
        "/admin-api/crm/contract/page": _page("管理后台 - CRM 合同", "Contract", [
            ("name", "string"), ("customerId", "integer"), ("auditStatus", "integer"), ("sceneType", "integer")]),
        "/admin-api/crm/contract/get": {"get": {"parameters": [
            {"in": "query", "name": "id", "required": True}]}},
        "/admin-api/crm/customer/page": _page("管理后台 - CRM 客户", "Customer", [
            ("name", "string"), ("sceneType", "integer")]),
        "/admin-api/erp/supplier/page": _page("管理后台 - ERP 供应商", "Supplier", [("name", "string")]),
        "/admin-api/bpm/process-instance/my-page": _page("管理后台 - 流程实例", "Instance", [("name", "string")]),
    },
    "components": {"schemas": {
        **_schemas("Contract", {
            "id": _prop("integer", "编号"), "name": _prop("string", "合同名称"),
            "customerId": _prop("integer", "客户编号"), "customerName": _prop("string", "客户名称"),
            "ownerUserId": _prop("integer", "负责人的用户编号"), "ownerUserName": _prop("string", "负责人名字"),
            "auditStatus": _prop("integer", "审批状态"), "totalPrice": _prop("number", "合同金额"),
            "orderDate": _prop("string", "下单日期", "date-time"),
            "discountPercent": _prop("number", "整单折扣，百分比"),
            "stageId": _prop("integer", "合同阶段编号"), "stageName": _prop("string", "阶段名字"),
            "endStatus": {"type": "integer"},
            "channelCodes": {"type": "array", "description": "已开通的渠道编码", "items": {"type": "string"}},
            "appSecret": _prop("string", "应用密钥")}),
        **_schemas("Customer", {"id": _prop("integer", "编号"), "name": _prop("string", "客户名称"),
                                "level": _prop("integer", "客户等级")}),
        **_schemas("Instance", {"id": _prop("string", "编号"), "name": _prop("string", "流程名称")}),
        **_schemas("Supplier", {"id": _prop("integer", "编号"), "name": _prop("string", "供应商名称"),
                                "status": _prop("integer", "开启状态")}),
    }},
}

MENUS = [
    {"name": "CRM 系统", "path": "/crm", "children": [
        {"name": "合同管理", "path": "contract", "component": "crm/contract/index"},
        {"name": "客户管理", "path": "customer", "component": "crm/customer/index"},
        {"name": "客户公海", "path": "customer/pool", "component": "crm/customer/pool/index"}]},
    {"name": "ERP 系统", "path": "/erp", "children": [
        {"name": "采购管理", "path": "purchase", "children": [
            {"name": "供应商信息", "path": "supplier", "component": "erp/purchase/supplier/index"}]}]},
    {"name": "工作流程", "path": "/bpm", "children": [
        {"name": "我的流程", "path": "task/my", "component": "bpm/processInstance/index"}]},
    {"name": "作者动态", "path": "https://example.com"},
]
PERMISSIONS = ["crm:contract:query", "crm:contract:create", "crm:customer:query", "erp:supplier:query",
               "bpm:process-instance:query"]
DICTS = [{"dictType": "crm_audit_status", "value": "0", "label": "未提交"},
         {"dictType": "crm_audit_status", "value": "20", "label": "审核通过"},
         {"dictType": "crm_customer_level", "value": "1", "label": "A （重点客户）"},
         {"dictType": "common_status", "value": "0", "label": "开启"},
         {"dictType": "common_status", "value": "1", "label": "关闭"},
         {"dictType": "crm_contract_end_status_type", "value": "1", "label": "赢单"},
         {"dictType": "crm_contract_end_status_type", "value": "2", "label": "输单"},
         {"dictType": "crm_contract_channel_code", "value": "web", "label": "网站"},
         {"dictType": "crm_contract_channel_code", "value": "app", "label": "App"}]
DICT_TYPES = [{"id": 82, "name": "CRM 合同结束状态类型", "type": "crm_contract_end_status_type"}]
USERS = [{"id": 257, "nickname": "评测员"}, {"id": 249, "nickname": "张伟"}]

CONTRACTS = {
    1: [{"id": 11, "name": "云帆企业版合同", "customerId": 5, "customerName": "杭州云帆科技有限公司",
         "ownerUserId": 257, "auditStatus": 20, "totalPrice": 29800.0, "orderDate": 1788192000000,
         "stageId": 3, "stageName": "履约中"}],
    2: [{"id": 12, "name": "瑞达实施服务合同", "customerId": 6, "customerName": "苏州瑞达精密机械有限公司",
         "ownerUserId": 249, "auditStatus": 0, "totalPrice": 7500.0, "orderDate": 1785513600000}],
    3: [{"id": 11, "name": "云帆企业版合同", "customerId": 5, "customerName": "杭州云帆科技有限公司",
         "ownerUserId": 257, "auditStatus": 20, "totalPrice": 29800.0, "orderDate": 1788192000000}],
}


class _Client:
    tenant = "1"

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def get(self, path, **params):
        self.calls.append((path, params))
        if path == "system/auth/get-permission-info":
            return {"menus": MENUS, "permissions": PERMISSIONS}
        if path == "system/dict-data/simple-list":
            return DICTS
        if path == "system/dict-type/simple-list":
            return DICT_TYPES
        if path == "system/user/simple-list":
            return USERS
        if path == "system/dept/simple-list":
            return []
        if path == "crm/contract/page":
            if params.get("sceneType") is None:
                raise AssertionError("a CRM list must be read with its scene")
            rows = [r for r in CONTRACTS[params["sceneType"]]
                    if not params.get("name") or params["name"] in r["name"]]
            return {"list": rows, "total": len(rows)}
        if path == "crm/contract/get":
            return next(r for rows in CONTRACTS.values() for r in rows if r["id"] == int(params["id"]))
        if path == "crm/customer/page":
            rows = [{"id": 5, "name": "云帆科技有限公司", "level": 1},
                    {"id": 6, "name": "苏州瑞达精密机械有限公司", "level": 1}]
            rows = [r for r in rows if not params.get("name") or params["name"] in r["name"]]
            return {"list": rows, "total": len(rows)}
        if path == "bpm/process-instance/my-page":
            return {"list": [{"id": "p1", "name": "请假"}], "total": 1}
        if path == "erp/supplier/page":
            rows = [{"id": 1, "name": "宁波海川电子有限公司", "status": 0}]
            return {"list": rows, "total": 1}
        raise AssertionError(path)


class YudaoGoalDataTests(unittest.TestCase):
    def test_ui_alias_keeps_api_field_and_requires_matching_endpoint(self):
        for endpoint, labels, alias in (
            ("/crm/contract/page", (("totalPrice", "含税金额"),), ("含税金额",)),
            ("/crm/other/page", (("totalPrice", "含税金额"),), ()),
            ("/crm/contract/page", (("totalPrice", "合同名称"),), ())):
            ui = UiSource(views={"crm/contract/index": ListView(endpoint=endpoint, column_labels=labels)})
            data = YudaoGoalData(_Client(), DOCS, ui=ui)
            entity = data.schema().entity("crm/contract")
            if entity is None:  # A conflicting endpoint cannot invent a readable entity.
                self.assertNotEqual(endpoint, "/crm/contract/page")
                continue
            field = next(f for f in entity.fields if f.key == "totalPrice")
            self.assertEqual(field.label, "合同金额")
            self.assertEqual(field.aliases, alias)
            self.assertEqual(set(f.key for f in entity.fields),
                set(f.key for f in YudaoGoalData(_Client(), DOCS).schema().entity("crm/contract").fields))

    def test_exact_local_timestamp_does_not_expand_to_a_day(self):
        rows = [dict(CONTRACTS[1][0], id=100+i, name=f"Synthetic {i}", orderDate=moment)
                for i, moment in enumerate(("2026-09-30T23:59:59", "2026-09-30T12:00:00", "2026-10-01T00:00:00", None))]
        with patch.object(self.data, "_matching", wraps=self.data._matching), patch.object(self.data, "_fetch", return_value=rows):
            for op, values, expected in (("=", ["2026-09-30 23:59:59"], 1),
                                         ("!=", ["2026-09-30 23:59:59"], 2),
                                         ("between", ["2026-09-30 12:00:00", "2026-09-30 23:59:59"], 2),
                                         ("=", ["2026-09-30 25:00:00"], None)):
                result = self.ask({"kind": "aggregate", "entity": "crm/contract", "metric": {
                    "op": "count", "filters": [{"field": "下单日期", "op": op, "values": values}]}})
                if expected is None:
                    self.assertTrue(result.problem)
                else:
                    self.assertFalse(result.problem)
                    self.assertEqual(result.aggregate.count, expected)

    def test_extreme_returns_all_ties_and_not_the_most_frequent_amount(self):
        rows = [dict(CONTRACTS[1][0], id=100+i, name=f"Synthetic {i}", totalPrice=amount)
                for i, amount in enumerate((0, 0, 0, 8000, 3500, 8000, None, -30))]
        with patch.object(self.data, "_matching", return_value=rows):
            for op, names in (("max", ("Synthetic 3", "Synthetic 5")), ("min", ("Synthetic 7",))):
                result = self.ask({"kind": "aggregate", "entity": "crm/contract",
                                   "metric": {"op": op, "field": "合同金额"}})
                self.assertFalse(result.problem)
                self.assertEqual(result.aggregate.records, names)
            self.assertTrue(self.ask({"kind": "aggregate", "entity": "crm/contract",
                                     "metric": {"op": "max", "field": "合同金额", "group_by": "合同金额"}}).problem)
            result = self.ask({"kind": "list", "entity": "crm/contract", "limit": 1,
                               "metric": {"op": "max", "field": "合同金额"}})
            self.assertFalse(result.problem)
            self.assertEqual(result.aggregate.records, ("Synthetic 3", "Synthetic 5"))

    def test_extreme_refuses_truncation_and_scope_above_the_bound(self):
        with patch.object(self.data, "count", return_value=4001), patch.object(self.data, "rows") as read:
            result = self.ask({"kind": "aggregate", "entity": "crm/contract",
                               "metric": {"op": "max", "field": "合同金额"}})
            self.assertTrue(result.problem)
            read.assert_not_called()
        with patch.object(self.data, "rows", return_value=()):
            result = self.ask({"kind": "aggregate", "entity": "crm/contract",
                               "metric": {"op": "max", "field": "合同金额"}})
            self.assertTrue(result.problem)

    def test_named_condition_that_matches_nothing_retains_the_scoped_result(self):
        result = self.ask({"kind": "fact", "entity": "crm/contract", "fields": ["合同金额"],
                           "metric": {"op": "count", "filters": [
                               {"field": "合同名称", "op": "=", "values": ["Synthetic absent contract"]}]}})
        self.assertTrue(result.no_matches)

    def test_date_exclusion_counts_outside_the_month_and_returns_the_same_records(self):
        declaration = {"kind": "aggregate", "entity": "crm/contract", "metric": {
            "op": "count", "filters": [{"field": "下单日期", "op": "!=", "values": ["2026-09"]}]}}
        counted = self.ask(declaration)
        self.assertFalse(counted.problem)
        self.assertEqual(counted.aggregate.count, 1)
        listed = self.ask({**declaration, "kind": "list"})
        self.assertEqual(listed.aggregate.count, 1)
        self.assertIn("瑞达实施服务合同", listed.aggregate.records[0])

    def test_known_amount_keeps_zero_and_negative_values_in_counts_lists_and_sum(self):
        class AmountClient(_Client):
            def get(self, path, **params):
                if path == "crm/contract/page":
                    rows = [dict(CONTRACTS[1][0], id=100+i, name=f"Synthetic {i}", totalPrice=amount)
                            for i, amount in enumerate((100, 0, -30, None))]
                    return {"list": rows, "total": len(rows)}
                return super().get(path, **params)
        port = DataGoalPort(YudaoGoalData(AmountClient(), DOCS))
        def ask(kind, op="count"):
            return port.resolve(GoalSpec.from_json({"deliverables": [{"kind": kind,
                "entity": "crm/contract", "metric": {"op": op, "field": "合同金额",
                    "filters": [{"field": "合同金额", "op": "!=", "values": []}]}}]})).deliverables[0]
        counted, listed, summed = ask("aggregate"), ask("list"), ask("aggregate", "sum")
        self.assertEqual([r.aggregate.count for r in (counted, listed, summed)], [3, 3, 3])
        self.assertEqual(len(listed.aggregate.records), 3)
        self.assertIn("70.00", summed.aggregate.sums[0][1])

    def test_total_refuses_missing_values_but_keeps_numeric_zero(self):
        for value in (None, "", float("nan"), float("inf")):
            rows = deepcopy(CONTRACTS[1] + CONTRACTS[2])
            rows[0]["totalPrice"] = value
            with self.subTest(value=value), patch.object(self.data, "_matching", return_value=rows):
                with self.assertRaises(DataRefused):
                    self.data.total("crm/contract", all_of(), "totalPrice")
        rows = deepcopy(CONTRACTS[1] + CONTRACTS[2])
        rows[0]["totalPrice"] = 0
        with patch.object(self.data, "_matching", return_value=rows):
            self.assertEqual((7500.0, ""), self.data.total("crm/contract", all_of(), "totalPrice"))

    def setUp(self):
        self.client = _Client()
        ui = UiSource(views={"crm/contract/index": ListView(columns=("name", "customerName", "totalPrice")),
                             "bpm/processInstance/index": ListView(endpoint="/bpm/process-instance/my-page")},
                      records={"/crm/contract": "/crm/contract/detail/{id}"})
        self.data = YudaoGoalData(self.client, DOCS, ui=ui)
        self.port = DataGoalPort(self.data)

    def ask(self, deliverable):
        return self.port.resolve(GoalSpec.from_json({"deliverables": [deliverable]})).deliverables[0]

    def test_complete_list_serves_narrow_queries_with_local_filtering(self):
        broad = self.data._matching("crm/contract", EVERYTHING)
        calls = len(self.client.calls)
        narrow = self.data._matching("crm/contract", Compare("name", "contains", "瑞达"))
        self.assertEqual([row["id"] for row in narrow], [12])
        self.assertEqual(len(self.client.calls), calls)
        self.assertEqual(len(broad), 2)

    def test_list_cache_expires_and_credential_change_invalidates_reads(self):
        with patch("web_operation_agent_yudao.goal.time.monotonic", return_value=100):
            self.data._fetch("crm/contract", EVERYTHING)
        calls = len(self.client.calls)
        with patch("web_operation_agent_yudao.goal.time.monotonic", return_value=115):
            self.data._fetch("crm/contract", EVERYTHING)
        self.assertEqual(len(self.client.calls), calls + 3)
        self.client.read_generation = 1
        self.data._fetch("crm/contract", EVERYTHING)
        # Renewal refreshes menus/dictionaries as well as the three CRM scene reads.
        self.assertEqual(len(self.client.calls), calls + 9)

    def test_cache_lifetime_starts_when_slow_read_finishes_and_is_bounded(self):
        self.data._keep_read(("slow",), (1,), 0)
        with patch("web_operation_agent_yudao.goal.time.monotonic", return_value=200):
            self.data._keep_read(("slow",), (2,), 0)
        with patch("web_operation_agent_yudao.goal.time.monotonic", return_value=214):
            self.assertEqual(self.data._cached_read(("slow",), 0), (2,))
        for i in range(130):
            self.data._keep_read((i,), (i,), 0)
        self.assertEqual(len(self.data._read), 128)
        self.assertIsNone(self.data._cached_read(("slow",), 0))

    def test_pages_become_entities_only_where_the_account_may_query_a_documented_list(self):
        schema = self.data.schema()
        self.assertEqual([p.key for p in schema.pages if p.entity], ["/crm/contract", "/crm/customer", "/erp/purchase/supplier",
                                                         "/bpm/task/my"])
        # A page that reads another list than its path's ("my-page") is that list, named
        # as its menu is.
        self.assertEqual(schema.page("/bpm/task/my").entity, "bpm/process-instance/my")
        self.assertEqual(schema.entity("bpm/process-instance/my").label, "我的流程")
        self.assertEqual(self.data.count("bpm/process-instance/my", all_of()), 1)
        # The actor still reaches the pool page, but it offers no records without a
        # documented readable endpoint; the supplier page's resource is its
        # last word, not its whole path.
        self.assertEqual("", schema.page("/crm/customer/pool").entity)
        self.assertEqual(schema.page("/erp/purchase/supplier").entity, "erp/supplier")
        self.assertEqual(schema.page("/crm/contract").operations, ("create",))
        index = self.port.index()
        self.assertEqual(index.page("/erp/purchase/supplier").breadcrumb, "ERP 系统 / 采购管理 / 供应商信息")
        self.assertIn("合同管理", index.entity("crm/contract").aliases)

    def test_department_number_is_a_readable_field_of_the_actor_visible_record(self):
        class DepartmentClient(_Client):
            def get(self, path, **params):
                if path == "system/dept/simple-list":
                    return [{"id": 122, "name": "华东销售部"}]
                return super().get(path, **params)

        data = YudaoGoalData(DepartmentClient(), DOCS)
        port = DataGoalPort(data)
        self.assertEqual(data.schema().entity("system/dept").field("id").label, "编号")
        result = port.resolve(GoalSpec.from_json({"deliverables": [{
            "kind": "fact", "entity": "system/dept", "record": "华东销售部", "fields": ["编号"]}]}))
        self.assertEqual(result.deliverables[0].facts[0].value, "122")

    def test_get_without_record_id_is_not_used_as_a_detail_read(self):
        docs = deepcopy(DOCS)
        docs["paths"]["/admin-api/pay/wallet/page"] = _page("管理后台 - 钱包", "Wallet", [])
        docs["paths"]["/admin-api/pay/wallet/get"] = {"get": {"parameters": [
            {"in": "query", "name": "userId", "required": True}]}}
        docs["components"]["schemas"].update(_schemas("Wallet", {
            "id": _prop("integer", "编号"), "balance": _prop("integer", "余额")}))

        class WalletClient(_Client):
            def get(self, path, **params):
                if path == "system/auth/get-permission-info":
                    return {"menus": [{"name": "支付", "path": "/pay", "children": [
                        {"name": "钱包", "path": "wallet", "component": "pay/wallet/index"}]}],
                        "permissions": ["pay:wallet:query"]}
                if path == "pay/wallet/page":
                    return {"list": [{"id": 1, "balance": 12840}], "total": 1}
                if path == "pay/wallet/get":
                    raise AssertionError("the userId lookup can create a wallet")
                return super().get(path, **params)

        data = YudaoGoalData(WalletClient(), docs, ui=UiSource(
            tables={"pay/wallet/components/WalletForm": {"balance": "余额"}}))
        self.assertEqual(data.schema().page("/pay/wallet").entity, "pay/wallet")
        self.assertEqual(data.read("pay/wallet", "1", ("balance",)).values["balance"], 12840)

    def test_a_documented_tree_page_reads_nested_records_with_a_bound(self):
        docs = deepcopy(DOCS)
        docs["paths"]["/admin-api/system/area/tree"] = {"get": {
            "tags": ["管理后台 - 地区"],
            "parameters": [{"in": "query", "name": "region", "schema": {"type": "integer"}}],
            "responses": {"200": {"content": {"*/*": {
                "schema": {"$ref": "#/components/schemas/RArea"}}}}}}}
        docs["components"]["schemas"].update({
            "RArea": {"properties": {"data": {"type": "array", "items": {
                "$ref": "#/components/schemas/Area"}}}},
            "Area": {"properties": {"id": _prop("integer", "编号"),
                                     "name": _prop("string", "地区名称")}},
        })

        class TreeClient(_Client):
            def get(self, path, **params):
                if path == "system/auth/get-permission-info":
                    return {"menus": [{"name": "系统", "path": "/system", "children": [
                        {"name": "地区管理", "path": "area", "component": "system/area/index"}]}],
                        "permissions": []}
                if path == "system/area/tree":
                    assert params["region"] == 1
                    return [{"id": 1, "name": "省", "children": [{"id": 2, "name": "市"}]}]
                return super().get(path, **params)

        data = YudaoGoalData(TreeClient(), docs,
                             ui=UiSource(views={"system/area/index": ListView(
                                 endpoint="/system/area/tree", fixed_params=(("region", 1),))}))
        self.assertEqual(data.schema().page("/system/area").entity, "system/area")
        self.assertEqual(data.count("system/area", all_of()), 2)
        limited = YudaoGoalData(TreeClient(), docs,
                                 ui=UiSource(views={"system/area/index": ListView(
                                     endpoint="/system/area/tree", fixed_params=(("region", 1),))}),
                                 max_rows=1)
        with self.assertRaises(DataRefused):
            limited.count("system/area", all_of())

    def test_fields_are_typed_from_the_documentation_and_named_from_the_dictionaries(self):
        contract = self.data.schema().entity("crm/contract")
        fields = {f.key: f for f in contract.fields}
        self.assertEqual((fields["customerId"].kind, fields["customerId"].label, fields["customerId"].relation),
                         ("link", "客户", "crm/customer"))
        self.assertEqual((fields["ownerUserId"].kind, fields["ownerUserId"].relation), ("link", "system/user"))
        self.assertNotIn("customerName", fields)
        self.assertEqual([c.label for c in fields["auditStatus"].choices], ["未提交", "审核通过"])
        self.assertEqual(fields["totalPrice"].kind, "money")
        self.assertEqual(fields["discountPercent"].label, "整单折扣")
        self.assertEqual(contract.total, "totalPrice")
        # The list's columns come first, in the list's order.
        self.assertEqual([f.key for f in contract.fields[:3]], ["name", "customerId", "totalPrice"])
        # A field named by the module's naming (crm_customer_level), and a status with no
        # dictionary by that naming read as yudao's common on/off status, as its pages do.
        self.assertEqual(self.data.schema().entity("crm/customer").field("level").kind, "choice")
        status = self.data.schema().entity("erp/supplier").field("status")
        self.assertEqual([c.label for c in status.choices], ["开启", "关闭"])

    def test_a_crm_list_is_read_under_every_scene_and_merged(self):
        rows = self.data.rows("crm/contract", all_of(), fields=("name",))
        self.assertEqual(sorted(r.display for r in rows), ["云帆企业版合同", "瑞达实施服务合同"])
        scenes = [p.get("sceneType") for path, p in self.client.calls if path == "crm/contract/page"]
        self.assertEqual(scenes, [1, 2, 3])

    def test_one_answer_reads_a_list_once(self):
        self.data.count("crm/contract", all_of())
        self.data.rows("crm/contract", all_of(), fields=("name",))
        scenes = [p.get("sceneType") for path, p in self.client.calls if path == "crm/contract/page"]
        self.assertEqual(scenes, [1, 2, 3])
        self.assertIn("values", self.data.capabilities())

    def test_a_condition_is_pushed_down_only_where_it_loses_no_match(self):
        self.data.count("crm/contract", all_of(Compare("customerId", "in", ("5",)),
                                               Compare("totalPrice", "gt", 1000)))
        params = [p for path, p in self.client.calls if path == "crm/contract/page"][-1]
        self.assertEqual(params.get("customerId"), "5")
        self.assertNotIn("totalPrice", params)

    def test_declarations_resolve_through_the_core(self):
        approved = self.ask({"kind": "aggregate", "entity": "crm/contract", "metric": {
            "op": "sum", "field": "合同金额", "filters": [{"field": "审批状态", "values": ["审核通过"]}]}})
        self.assertEqual((approved.aggregate.count, approved.aggregate.sums), (1, (("合同金额", "29,800.00"),)))
        mine = self.ask({"kind": "list", "entity": "crm/contract", "fields": ["客户"], "metric": {
            "op": "count", "filters": [{"field": "负责人", "values": ["张伟"]}]}})
        self.assertEqual(mine.aggregate.records, ("瑞达实施服务合同 — 客户: 苏州瑞达精密机械有限公司",))
        fact = self.ask({"kind": "fact", "entity": "crm/contract", "record": "云帆企业版合同",
                         "fields": ["审批状态", "下单日期"]})
        self.assertEqual([f.value for f in fact.facts], ["审核通过", "2026-09-01"])
        page = self.ask({"kind": "page", "entity": "crm/contract", "record": "云帆企业版合同"})
        self.assertEqual(page.page.url, "/crm/contract/detail/11")
        # A collection fallback is labelled as the collection, never as record detail.
        supplier = self.ask({"kind": "page", "entity": "erp/supplier", "record": "宁波海川"})
        self.assertEqual(supplier.page.url, "/erp/purchase/supplier")
        self.assertEqual(supplier.page.missing_record, "宁波海川电子有限公司")
        self.assertNotEqual(supplier.page.label, "宁波海川")
        # "Open the contract list": the page's one way of showing records is the list,
        # so there is no view to switch to or find missing.
        listed = self.ask({"kind": "page", "entity": "crm/contract",
                           "page": {"key": "/crm/contract", "view": "list"}})
        self.assertEqual((listed.problem, listed.page.view, listed.page.missing_view), ("", "", ""))


class UndescribedCodeTests(unittest.TestCase):
    def test_a_code_the_api_leaves_undescribed_is_named_by_its_dictionary(self):
        data = YudaoGoalData(_Client(), DOCS)
        field = next(f for f in data.schema().entity("crm/contract").fields if f.key == "endStatus")
        self.assertEqual((field.label, field.kind), ("合同结束状态", "choice"))
        self.assertEqual([c.label for c in field.choices], ["赢单", "输单"])

    def test_several_codes_read_as_their_names_and_secrets_are_never_read(self):
        client = _Client()
        CONTRACTS[1][0]["channelCodes"] = ["web", "app"]
        CONTRACTS[1][0]["appSecret"] = "s3cr3t"
        try:
            data = YudaoGoalData(client, DOCS)
            fields = {f.key: f for f in data.schema().entity("crm/contract").fields}
            self.assertEqual(fields["channelCodes"].label, "已开通的渠道")
            self.assertNotIn("appSecret", fields)
            row = data.read("crm/contract", "11", ("channelCodes",))
            self.assertEqual(row.shown["channelCodes"], "网站、App")
        finally:
            CONTRACTS[1][0].pop("channelCodes"); CONTRACTS[1][0].pop("appSecret")


class DeclarationRepairTests(unittest.TestCase):
    """What the resolver tells the declarer when a name or field is not where declared."""

    def setUp(self):
        self.port = DataGoalPort(YudaoGoalData(_Client(), DOCS))

    def ask(self, deliverable):
        return self.port.resolve(GoalSpec.from_json({"deliverables": [deliverable]})).deliverables[0]

    def test_a_fact_about_an_unnamed_record_is_the_one_in_scope_or_asks_which(self):
        several = self.ask({"kind": "fact", "entity": "crm/contract", "fields": ["合同金额"]})
        self.assertEqual(sorted(several.candidates), ["云帆企业版合同", "瑞达实施服务合同"])
        one = self.ask({"kind": "fact", "entity": "crm/contract", "fields": ["合同金额"],
                        "metric": {"op": "count", "filters": [{"field": "负责人", "values": ["张伟"]}]}})
        self.assertEqual([f.value for f in one.facts], ["7,500.00"])

    def test_the_records_pointing_at_a_named_record_are_the_ones_meant(self):
        contracts = self.ask({"kind": "page", "entity": "crm/contract", "record": "苏州瑞达精密机械有限公司"})
        self.assertEqual(contracts.page.missing_record, "瑞达实施服务合同")
        self.assertEqual(contracts.page.label, "CRM 系统 / 合同管理")

    def test_an_empty_list_asked_for_as_a_whole_is_an_answer(self):
        suppliers = self.ask({"kind": "list", "entity": "crm/contract", "metric": {
            "op": "count", "filters": [{"field": "负责人", "values": ["评测员"]},
                                       {"field": "审批状态", "values": ["未提交"]}]}})
        self.assertFalse(suppliers.problem)
        self.assertEqual((suppliers.aggregate.count, suppliers.aggregate.records), (0, ()))

    def test_words_before_a_name_are_not_part_of_it(self):
        fact = self.ask({"kind": "fact", "entity": "crm/contract", "record": "我的云帆企业版合同",
                         "fields": ["合同金额"]})
        self.assertEqual([f.value for f in fact.facts], ["29,800.00"])

    def test_a_name_that_is_another_entitys_record_says_which(self):
        missing = self.ask({"kind": "fact", "entity": "erp/supplier", "record": "苏州瑞达精密机械",
                            "fields": ["开启状态"]})
        self.assertTrue(missing.problem)
        self.assertIn("entity crm/customer", missing.repair)

    def test_a_name_several_records_of_another_entity_hold_points_there(self):
        # Customers' names begin with "苏州瑞达" too, but only one does: it is that record's.
        missing = self.ask({"kind": "fact", "entity": "erp/supplier", "record": "瑞达",
                            "fields": ["开启状态"]})
        self.assertTrue(missing.problem)
        # Two contract names hold "合同" only further in: nobody is pointed at.
        nowhere = self.ask({"kind": "fact", "entity": "erp/supplier", "record": "合同",
                            "fields": ["开启状态"]})
        self.assertEqual(nowhere.elsewhere, ())

    def test_a_field_the_entity_does_not_have_asks_for_the_declaration_again(self):
        fact = self.ask({"kind": "fact", "entity": "crm/contract", "record": "云帆企业版合同",
                         "fields": ["合同金额", "付款条件"]})
        self.assertEqual([f.value for f in fact.facts], ["29,800.00"])
        self.assertIn("no field named '付款条件'", fact.repair)


class _StrictDetailClient(_Client):
    """A detail endpoint that checks a record more strictly than its list, and leaves a
    number's name unfilled, as some of yudao's do."""

    def get(self, path, **params):
        if path == "crm/contract/get":
            if int(params["id"]) == 12:
                raise DataRefused("crm/contract/get: 1020007001 没有权限")
            row = dict(super().get(path, **params))
            row["stageName"] = None
            return row
        return super().get(path, **params)


class DetailReadTests(unittest.TestCase):
    def setUp(self):
        self.data = YudaoGoalData(_StrictDetailClient(), DOCS)
        self.port = DataGoalPort(self.data)

    def ask(self, deliverable):
        return self.port.resolve(GoalSpec.from_json({"deliverables": [deliverable]})).deliverables[0]

    def test_a_number_without_a_record_of_its_own_is_shown_by_its_name(self):
        fields = {f.key: f for f in self.data.schema().entity("crm/contract").fields}
        self.assertNotIn("stageId", fields)
        self.assertEqual((fields["stageName"].label, fields["stageName"].kind), ("合同阶段", "text"))
        # The detail leaves the name unfilled; the list the account reads it from has it.
        fact = self.ask({"kind": "fact", "entity": "crm/contract", "record": "云帆企业版合同",
                         "fields": ["合同阶段"]})
        self.assertEqual([f.value for f in fact.facts], ["履约中"])

    def test_a_fact_whose_fields_are_all_empty_names_the_fields_that_hold_a_value(self):
        fact = self.ask({"kind": "fact", "entity": "crm/contract", "record": "云帆企业版合同",
                         "fields": ["整单折扣"]})
        self.assertEqual(fact.empty, ("整单折扣",))
        self.assertIn("合同金额", fact.repair)
        self.assertNotIn("整单折扣,", fact.repair)
        # One field with a value is an answer: nothing to repair.
        both = self.ask({"kind": "fact", "entity": "crm/contract", "record": "云帆企业版合同",
                         "fields": ["整单折扣", "合同金额"]})
        self.assertEqual((both.empty, both.repair), (("整单折扣",), ""))

    def test_a_record_the_detail_refuses_is_read_as_the_accounts_list_shows_it(self):
        fact = self.ask({"kind": "fact", "entity": "crm/contract", "record": "瑞达实施服务合同",
                         "fields": ["合同金额"]})
        self.assertEqual([f.value for f in fact.facts], ["7,500.00"])


class _LinesClient(_Client):
    """A contract's detail carries its products, as yudao's does."""

    def get(self, path, **params):
        row = super().get(path, **params)
        if path == "crm/contract/get":
            products = {11: [{"id": 1, "productName": "企业版订阅", "count": 2.0, "productPrice": 14900.0},
                             {"id": 2, "productName": "实施服务", "count": 1.0, "productPrice": 3000.0}],
                        12: [{"id": 3, "productName": "实施服务", "count": 3.0, "productPrice": 2500.0}]}
            row = {**row, "products": products[int(params["id"])]}
        return row


class LineTests(unittest.TestCase):
    def setUp(self):
        ui = UiSource(tables={"crm/contract/components/ContractProductForm": {
            "productName": "产品名称", "count": "数量", "productPrice": "价格（元）", "id": "操作"}})
        self.data = YudaoGoalData(_LinesClient(), DOCS, ui=ui)
        self.port = DataGoalPort(self.data)

    def test_line_dictionary_labels_use_actor_read_values_and_keep_raw_keys(self):
        class CodedLines(_LinesClient):
            def get(self, path, **params):
                result = super().get(path, **params)
                if path == "system/dict-data/simple-list":
                    return result + [{"dictType": "line_measure", "value": "1", "label": "箱"}]
                if path == "crm/contract/get":
                    result = {**result, "products": [dict(r, measureCode=1) for r in result["products"]]}
                return result

        ui = UiSource(tables={"crm/contract/detail/Items": {
            "productName": "产品名称", "count": "数量", "measureCode": "单位"}},
            table_dicts={"crm/contract/detail/Items": {"measureCode": "line_measure"}})
        data = YudaoGoalData(CodedLines(), DOCS, ui=ui)
        schema = data.schema().entity("crm/contract/products")
        self.assertEqual(schema.field("measureCode").kind, "choice")
        self.assertNotIn("measureCode", data._lines[schema.key].columns)
        rows = data.rows(schema.key, Compare("__owner", "eq", "12"), fields=("measureCode",))
        self.assertEqual([(r.values["measureCode"], r.shown["measureCode"]) for r in rows], [("1", "箱")])
        # An unregistered value stays explicit; a known key's label is never reused.
        get = data._client.get
        def unknown(path, **params):
            result = get(path, **params)
            if path == "crm/contract/get":
                result = {**result, "products": [dict(r, measureCode=99) for r in result["products"]]}
            return result
        data._client.get = unknown
        data = YudaoGoalData(data._client, DOCS, ui=ui)
        rows = data.rows(schema.key, Compare("__owner", "eq", "12"), fields=("measureCode",))
        self.assertEqual(rows[0].shown["measureCode"], "99")

    def test_missing_actor_dictionary_does_not_invent_line_labels(self):
        class CodedLines(_LinesClient):
            def get(self, path, **params):
                result = super().get(path, **params)
                if path == "crm/contract/get":
                    result = {**result, "products": [dict(r, measureCode=1) for r in result["products"]]}
                return result
        ui = UiSource(tables={"crm/contract/detail/Items": {
            "productName": "产品名称", "count": "数量", "measureCode": "单位"}},
            table_dicts={"crm/contract/detail/Items": {"measureCode": "unread_dictionary"}})
        data = YudaoGoalData(CodedLines(), DOCS, ui=ui)
        self.assertEqual(data.schema().entity("crm/contract/products").field("measureCode").kind, "number")
        rows = data.rows("crm/contract/products", Compare("__owner", "eq", "12"), fields=("measureCode",))
        self.assertEqual(rows[0].shown["measureCode"], "1")

    def test_a_detail_s_line_table_is_an_entity_of_its_own(self):
        lines = self.data.schema().entity("crm/contract/products")
        self.assertEqual(lines.lines_of, "crm/contract")
        self.assertEqual([(f.key, f.label) for f in lines.fields],
                         [("__owner", "合同"), ("productName", "产品名称"), ("count", "数量"),
                          ("productPrice", "价格")])
        owner = self.data.schema().entity("crm/contract").field("products")
        self.assertEqual((owner.kind, owner.relation), ("lines", "crm/contract/products"))
        self.assertIn("lines", self.data.capabilities())

    def test_lines_are_read_for_one_record_or_across_all(self):
        from web_operation_agent.goal.data import Compare
        one = self.data.rows("crm/contract/products", Compare("__owner", "eq", "12"), fields=("count",))
        self.assertEqual([(r.display, r.values["count"]) for r in one], [("实施服务", 3.0)])
        total, _ = self.data.total("crm/contract/products",
                                   Compare("productName", "contains", "实施服务"), "count")
        self.assertEqual(total, 4.0)

    def test_an_unreadable_owner_refuses_counts_and_totals_instead_of_omitting_its_lines(self):
        self.data.schema()
        get = self.data._client.get

        def refuse(path, **params):
            if path == "crm/contract/get" and str(params.get("id")) == "12":
                raise DataRefused("detail denied", reason="无权限")
            return get(path, **params)

        self.data._client.get = refuse
        for read in (lambda: self.data.count("crm/contract/products", all_of()),
                     lambda: self.data.total("crm/contract/products", all_of(), "count")):
            with self.assertRaises(DataRefused) as caught:
                read()
            self.assertEqual(caught.exception.reason, "无权限")
        self.data._client.get = get
        self.assertEqual(self.data.total("crm/contract/products", all_of(), "count"), (6.0, ""))

    def test_a_missing_line_table_is_not_an_empty_line_table(self):
        self.data.schema()
        get = self.data._client.get
        self.data._client.get = lambda path, **params: {} if path == "crm/contract/get" else get(path, **params)
        with self.assertRaises(DataRefused):
            self.data.count("crm/contract/products", Compare("__owner", "eq", "12"))

    def test_line_prices_use_the_column_s_declared_unit(self):
        data = YudaoGoalData(_LinesClient(), DOCS, ui=UiSource(tables={
            "crm/contract/components/ContractProductForm": {
                "productName": "产品名称", "count": "数量", "productPrice": "价格（分）"}}))
        data.schema()
        where = Compare("__owner", "eq", "12")
        row = data.rows("crm/contract/products", where, fields=("productPrice",))[0]
        self.assertEqual(row.values["productPrice"], 25.0)
        self.assertEqual(row.shown["productPrice"], "25.00 元")
        self.assertEqual(data.total("crm/contract/products", where, "productPrice"), (25.0, "元"))

    def test_raw_line_api_units_take_precedence_over_formatted_column_units(self):
        docs = deepcopy(DOCS)
        docs["paths"]["/admin-api/crm/contract/get"]["get"]["responses"] = {
            "200": {"content": {"application/json": {"schema": {"properties": {
                "data": {"$ref": "#/components/schemas/ContractDetail"}}}}}}}
        docs["components"]["schemas"].update({
            "ContractDetail": {"properties": {"products": {"type": "array", "items": {
                "$ref": "#/components/schemas/ContractLine"}}}},
            "ContractLine": {"properties": {"productPrice": {
                "type": "integer", "description": "单价，单位：分"}}}})
        data = YudaoGoalData(_LinesClient(), docs, ui=self.data._ui)
        data.schema()
        where = Compare("__owner", "eq", "12")
        row = data.rows("crm/contract/products", where, fields=("productPrice",))[0]
        self.assertEqual(row.values["productPrice"], 25.0)
        self.assertEqual(row.shown["productPrice"], "25.00 元")
        self.assertEqual(data.total("crm/contract/products", where, "productPrice"), (25.0, "元"))


class MoneyUnitTests(unittest.TestCase):
    def test_cents_and_yuan_use_one_scale_for_facts_filters_and_totals(self):
        for unit, expected, shown in (("分", 298.0, "298.00 元"),
                                      ("元", 29800.0, "29,800.00 元")):
            with self.subTest(unit=unit):
                docs = deepcopy(DOCS)
                docs["components"]["schemas"]["Contract"]["properties"]["totalPrice"]["description"] = \
                    f"合同金额，单位：{unit}"
                docs["paths"]["/admin-api/crm/contract/page"]["get"]["parameters"].append(
                    {"in": "query", "name": "totalPrice", "schema": {"type": "integer"}})
                client = _Client()
                data = YudaoGoalData(client, docs)
                data.schema()
                row = data.read("crm/contract", "11", ("totalPrice",))
                self.assertEqual(row.values["totalPrice"], expected)
                self.assertEqual(row.shown["totalPrice"], shown)
                where = Compare("totalPrice", "eq", expected)
                self.assertEqual(data.count("crm/contract", where), 1)
                self.assertEqual(data.total("crm/contract", where, "totalPrice"), (expected, "元"))
                if unit == "分":
                    self.assertFalse(any("totalPrice" in params for path, params in client.calls
                                         if path == "crm/contract/page"))
                self.assertEqual(data.total("crm/contract", Compare("totalPrice", "lt", 0),
                                            "totalPrice"), (0.0, "元"))

    def test_an_undeclared_currency_is_not_invented(self):
        data = YudaoGoalData(_Client(), DOCS)
        data.schema()
        self.assertEqual(data.read("crm/contract", "11", ("totalPrice",)).shown["totalPrice"], "29,800.00")
        self.assertEqual(data.total("crm/contract", Compare("@id", "eq", "11"), "totalPrice"),
                         (29800.0, ""))


class _BranchClient(_Client):
    """A customer whose name holds another's whole: "云帆科技有限公司杭州分公司", with no contract."""

    def get(self, path, **params):
        if path == "crm/customer/page":
            rows = [{"id": 5, "name": "云帆科技有限公司", "level": 1},
                    {"id": 7, "name": "云帆科技有限公司杭州分公司", "level": 1},
                    {"id": 6, "name": "苏州瑞达精密机械有限公司", "level": 1}]
            rows = [r for r in rows if not params.get("name") or params["name"] in r["name"]]
            return {"list": rows, "total": len(rows)}
        return super().get(path, **params)


class _ScopedBranchClient(_BranchClient):
    def get(self, path, **params):
        result = deepcopy(super().get(path, **params))
        if path == "crm/contract/page":
            for row in result["list"]:
                if row.get("customerId") == 5:
                    row["customerName"] = "云帆科技有限公司"
        return result


class ExactAmongLongerTests(unittest.TestCase):
    def test_an_ambiguous_link_offers_exact_names_for_repair_without_choosing(self):
        port = DataGoalPort(YudaoGoalData(_BranchClient(), DOCS))
        declaration = {"kind": "list", "entity": "crm/contract", "metric": {
            "op": "count", "filters": [{"field": "客户", "values": ["云帆科技有限公司"]}]}}
        unresolved = port.resolve(GoalSpec.from_json({"deliverables": [declaration]})).deliverables[0]
        self.assertIsNone(unresolved.aggregate)
        self.assertEqual(len(unresolved.candidates), 2)
        self.assertIn("exclusion", unresolved.repair)
        self.assertTrue(all(name in unresolved.repair for name in unresolved.candidates))
        # An explicit user-given exclusion can narrow the existing complete scope.
        excluded = next(name for name in unresolved.candidates if name != "云帆科技有限公司")
        declaration["metric"]["filters"].append({"field": "客户", "op": "!=", "values": [excluded]})
        port = DataGoalPort(YudaoGoalData(_ScopedBranchClient(), DOCS))
        result = port.resolve(GoalSpec.from_json({"deliverables": [declaration]})).deliverables[0]
        self.assertIsNotNone(result.aggregate)
        self.assertFalse(result.candidates)

    def test_a_name_condition_binds_the_only_candidate_in_the_complete_scope(self):
        port = DataGoalPort(YudaoGoalData(_ScopedBranchClient(), DOCS))
        resolved = port.resolve(GoalSpec.from_json({"deliverables": [{
            "kind": "list", "entity": "crm/contract", "metric": {
                "op": "count", "filters": [{"field": "客户", "values": ["云帆科技有限公司"]}]}}]})).deliverables[0]
        # The branch has no contract in the actor's complete scope.
        self.assertEqual(resolved.candidates, ())
        self.assertEqual(resolved.aggregate.count, 1)
        self.assertIn("云帆企业版合同", resolved.aggregate.records[0])

    def test_truncated_or_inconsistent_scope_cannot_prove_uniqueness(self):
        data = YudaoGoalData(_ScopedBranchClient(), DOCS)
        port = DataGoalPort(data)
        spec = GoalSpec.from_json({"deliverables": [{"kind": "list", "entity": "crm/contract",
            "metric": {"op": "count", "filters": [{"field": "客户", "values": ["云帆科技有限公司"]}]}}]})
        for count in (3, 201):
            with self.subTest(count=count), patch.object(data, "count", return_value=count):
                resolved = port.resolve(spec).deliverables[0]
                self.assertEqual(len(resolved.candidates), 2)

    def test_an_unusable_other_condition_cannot_be_discarded_to_prove_uniqueness(self):
        port = DataGoalPort(YudaoGoalData(_ScopedBranchClient(), DOCS))
        spec = GoalSpec.from_json({"deliverables": [{"kind": "list", "entity": "crm/contract",
            "metric": {"op": "count", "filters": [
                {"field": "客户", "values": ["云帆科技有限公司"]},
                {"field": "Unknown Field", "values": ["Only one"]}]}}]})
        resolved = port.resolve(spec).deliverables[0]
        self.assertEqual(len(resolved.candidates), 2)

    def test_a_negated_ambiguous_name_is_not_rewritten_as_a_positive_binding(self):
        port = DataGoalPort(YudaoGoalData(_ScopedBranchClient(), DOCS))
        resolved = port.resolve(GoalSpec.from_json({"deliverables": [{"kind": "list",
            "entity": "crm/contract", "metric": {"op": "count", "filters": [
                {"field": "客户", "op": "!=", "values": ["云帆科技有限公司"]}]}}]})).deliverables[0]
        self.assertEqual(len(resolved.candidates), 2)
        self.assertIsNone(resolved.aggregate)


class _TwinClient(_Client):
    """Two contracts of one name, at two customers."""

    def get(self, path, **params):
        if path == "crm/contract/page":
            rows = [{"id": 11, "name": "云帆企业版合同", "customerId": 5, "customerName": "杭州云帆科技有限公司",
                     "ownerUserId": 257, "auditStatus": 20, "totalPrice": 29800.0},
                    {"id": 13, "name": "云帆企业版合同", "customerId": 6, "customerName": "苏州瑞达精密机械有限公司",
                     "ownerUserId": 257, "auditStatus": 0, "totalPrice": 100.0}]
            rows = [r for r in rows if not params.get("name") or params["name"] in r["name"]]
            return {"list": rows, "total": len(rows)}
        if path == "crm/contract/get":
            return next(r for r in self.get("crm/contract/page", sceneType=1)["list"]
                        if r["id"] == int(params["id"]))
        return super().get(path, **params)


class SameNameTests(unittest.TestCase):
    def test_condition_candidates_keep_owners_and_round_trip_to_exact_records(self):
        data = YudaoGoalData(_TwinClient(), DOCS)
        port = DataGoalPort(data)
        result = port.resolve(GoalSpec.from_json({"deliverables": [{"kind": "fact",
            "entity": "crm/contract", "fields": ["合同金额"], "metric": {"op": "count",
            "filters": [{"field": "合同名称", "values": ["云帆企业版合同"]}]}}]})).deliverables[0]
        self.assertEqual(len(result.candidates), 2)
        self.assertEqual([data.find("crm/contract", name)[0].id for name in result.candidates], ["11", "13"])
        self.assertTrue(all("（" in name for name in result.candidates))

    def test_identical_names_and_owners_use_exact_actor_visible_ids(self):
        data = YudaoGoalData(_TwinClient(), DOCS)
        data.schema()
        rows = [{"id": 11, "name": "Twin", "customerName": "Same"},
                {"id": 13, "name": "Twin", "customerName": "Same"}]
        with patch.object(data, "_fetch", return_value=rows):
            names = [r.display for r in data.find("crm/contract", "Twin", limit=1)]
            self.assertEqual(names, ["Twin（Same；ID=11）"])
            chosen = data.find("crm/contract", "Twin（Same；ID=13）")
            self.assertEqual([r.id for r in chosen], ["13"])
            found = data.rows("crm/contract", all_of())
            self.assertEqual([r.display for r in found], ["Twin（Same；ID=11）", "Twin（Same；ID=13）"])

    def test_records_of_one_name_are_told_apart_by_whose_they_are(self):
        data = YudaoGoalData(_TwinClient(), DOCS)
        data.schema()
        self.assertEqual([r.display for r in data.find("crm/contract", "云帆企业版合同")],
                         ["云帆企业版合同（杭州云帆科技有限公司）", "云帆企业版合同（苏州瑞达精密机械有限公司）"])
        chosen = data.find("crm/contract", "云帆企业版合同（苏州瑞达精密机械有限公司）")
        self.assertEqual([(r.id, r.name) for r in chosen], [("13", "云帆企业版合同")])
        asked = DataGoalPort(data).resolve(GoalSpec.from_json({"deliverables": [{
            "kind": "fact", "entity": "crm/contract", "record": "云帆企业版合同", "fields": ["合同金额"]}]}))
        self.assertEqual(len(asked.deliverables[0].candidates), 2)


class NameLookupTests(unittest.TestCase):
    def setUp(self):
        self.port = DataGoalPort(YudaoGoalData(_Client(), DOCS))

    def test_a_whole_name_is_its_record_and_a_shared_beginning_is_nobody(self):
        found = self.port.locate_names(("云帆科技有限公司", "张伟"), entities=("crm/contract",))
        self.assertEqual([(t, e) for t, e, _n in found],
                         [("云帆科技有限公司", "crm/customer"), ("张伟", "system/user")])
        # "云帆" begins a contract's name and a customer's: neither is taken.
        self.assertEqual(self.port.locate_names(("云帆",), entities=("crm/contract",)), ())
        # One entity's name alone begins with it: that record.
        self.assertEqual(self.port.locate_names(("瑞达",), entities=("crm/contract",)),
                         (("瑞达", "crm/contract", "瑞达实施服务合同"),))


if __name__ == "__main__":
    unittest.main()


class TokenClientTests(unittest.TestCase):
    """A client made from a person's own token never logs in, and takes a newer token."""

    def test_an_expired_token_is_refused_not_logged_in_again(self):
        from web_operation_agent_yudao.client import YudaoClient
        client = YudaoClient.for_token("http://yudao.invalid", "t1", "1")
        seen = []

        def request(method, path, *, params=None, body=None, token=None):
            seen.append((method, path, token))
            return 401, {"code": 401, "msg": "expired"}
        client._request = request
        with self.assertRaises(DataRefused):
            client.get("system/auth/get-permission-info")
        self.assertEqual(seen, [("GET", "system/auth/get-permission-info", "t1")])
        self.assertEqual(client.read_generation, 0)
        client.renew("t1")
        self.assertEqual(client.read_generation, 0)
        client.renew("t2")
        self.assertEqual(client.read_generation, 1)
        with self.assertRaises(DataRefused):
            client.get("crm/customer/page")
        self.assertEqual(seen[-1][2], "t2")
        self.assertEqual(client.tenant, "1")
        with self.assertRaises(ValueError):
            YudaoClient.for_token("http://yudao.invalid", "", "1")


def _required_page(tag, item, required):
    """A documented list whose named query parameters the endpoint itself requires."""
    return {"get": {"tags": [tag], "parameters": [
        {"in": "query", "name": name, "required": True, "schema": {"type": "integer"}}
        for name in required],
        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": f"#/components/schemas/R{item}"}}}}}}}


def _array_list(tag, item, props):
    """A documented unpaged list: `CommonResult<List<Item>>`."""
    return {"paths": {f"/admin-api/{tag}": {"get": {"tags": ["管理后台 - FMS 账套"], "parameters": [
        {"in": "query", "name": "companyName", "schema": {"type": "string"}}],
        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": f"#/components/schemas/R{item}"}}}}}}}},
        "components": {"schemas": {
            f"R{item}": {"properties": {"data": {"type": "array",
                                                 "items": {"$ref": f"#/components/schemas/{item}"}}}},
            item: {"properties": props}}}}


FMS_MENUS = [
    {"name": "FMS 财务管理", "path": "/fms", "children": [
        {"name": "账套", "path": "config/account-set", "component": "fms/config/accountSet/index"},
        {"name": "凭证管理", "path": "voucher", "component": "fms/voucher/index"}]},
]

ACCOUNT_SET_DOCS = _array_list("fms/config/account-set/list", "AccountSet",
                               {"id": _prop("integer", "账套编号"),
                                "companyName": _prop("string", "公司名称")})
FMS_DOCS = {
    "paths": {
        **ACCOUNT_SET_DOCS["paths"],
        "/admin-api/fms/voucher/page": _required_page("管理后台 - FMS 凭证", "Voucher", ["accountSetId"]),
    },
    "components": {"schemas": {
        **ACCOUNT_SET_DOCS["components"]["schemas"],
        **_schemas("Voucher", {"id": _prop("integer", "编号"), "digest": _prop("string", "摘要"),
                               "accountSetId": _prop("integer", "账套编号")}),
    }},
}


class _FmsClient(_Client):
    """The accounting pages' endpoints: a list read under the account set it belongs to."""

    def __init__(self, account_sets):
        super().__init__()
        self.account_sets = account_sets

    def get(self, path, **params):
        if path == "system/auth/get-permission-info":
            # No permission names these resources: the account's menu is the evidence it
            # may open the page, and the page's own address names its list.
            return {"menus": FMS_MENUS, "permissions": []}
        if path == "fms/config/account-set/list":
            self.calls.append((path, params))
            return list(self.account_sets)
        if path == "fms/voucher/page":
            self.calls.append((path, params))
            if params.get("accountSetId") is None:
                raise DataRefused("fms/voucher/page: 400 请求参数不正确:账套不能为空")
            return {"list": [{"id": 1, "digest": "提现", "accountSetId": params["accountSetId"]}], "total": 1}
        return super().get(path, **params)


class ParentSelectionTests(unittest.TestCase):
    def test_complete_list_cache_never_crosses_required_parent_scope(self):
        client = _FmsClient([{"id": 1, "companyName": "One"}, {"id": 2, "companyName": "Two"}])
        data = YudaoGoalData(client, FMS_DOCS)
        first = data._fetch("fms/voucher", Compare("accountSetId", "eq", "1"))
        second = data._fetch("fms/voucher", Compare("accountSetId", "eq", "2"))
        self.assertEqual(str(first[0]["accountSetId"]), "1")
        self.assertEqual(str(second[0]["accountSetId"]), "2")
        self.assertEqual(len([p for path, p in client.calls if path == "fms/voucher/page"]), 2)

    def test_a_page_whose_address_names_a_multi_word_list_endpoint_is_that_list(self):
        # `/fms/config/account-set` reads `/admin-api/fms/config/account-set/list`: two
        # words deep, a `/list` rather than a `/page`, and a permission that names neither.
        data = YudaoGoalData(_FmsClient([{"id": 1, "companyName": "示例信息技术有限公司"}]), FMS_DOCS)
        schema = data.schema()
        self.assertEqual(schema.page("/fms/config/account-set").entity, "fms/config/account-set")
        self.assertEqual(data._entities["fms/config/account-set"].list_path, "fms/config/account-set/list")
        self.assertEqual([(r.id, r.values["companyName"]) for r in
                          data.rows("fms/config/account-set", all_of(), fields=("companyName",))],
                         [("1", "示例信息技术有限公司")])

    def test_a_required_parent_the_question_did_not_name_is_the_only_one_the_account_reads(self):
        # The account reads one account set, which is the one the application itself would
        # show; the vouchers are read under it without the question naming it.
        client = _FmsClient([{"id": 1, "companyName": "示例信息技术有限公司"}])
        data = YudaoGoalData(client, FMS_DOCS)
        data.schema()
        rows = data.rows("fms/voucher", all_of(), fields=("digest",))
        self.assertEqual([(r.id, r.values["digest"]) for r in rows], [("1", "提现")])
        self.assertEqual([p for path, p in client.calls if path == "fms/voucher/page"],
                         [{"pageNo": 1, "pageSize": 200, "accountSetId": 1}])
        self.assertEqual(data.count("fms/voucher", all_of()), 1)

    def test_several_parents_are_the_person_s_choice_and_the_read_refuses(self):
        client = _FmsClient([{"id": 1, "companyName": "示例信息技术有限公司"},
                             {"id": 2, "companyName": "示例第二有限公司"}])
        data = YudaoGoalData(client, FMS_DOCS)
        data.schema()
        with self.assertRaises(DataRefused):
            data.rows("fms/voucher", all_of())
        self.assertEqual([p for path, p in client.calls if path == "fms/voucher/page"],
                         [{"pageNo": 1, "pageSize": 200}])


class RefusalReasonTests(unittest.TestCase):
    def test_transport_failures_are_classified_without_reading_error_bodies(self):
        import urllib.error
        from web_operation_agent_yudao.client import YudaoClient
        client = YudaoClient.for_token("http://synthetic.invalid", "synthetic-token", "1")
        errors = (urllib.error.HTTPError("http://synthetic.invalid", 503, "private body", {}, None),
                  urllib.error.URLError("private transport details"), TimeoutError())
        for error in errors:
            with self.subTest(type=type(error)), patch("urllib.request.urlopen", side_effect=error), \
                    self.assertRaises(DataRefused) as caught:
                client.get("synthetic/read")
            self.assertTrue(caught.exception.read_failed)
            self.assertFalse(caught.exception.permission_denied)
            self.assertNotIn("private", str(caught.exception))

    def test_response_codes_distinguish_service_denial_and_business_validation(self):
        from web_operation_agent_yudao.client import YudaoClient
        for code, denied, failed in ((403, True, False), (503, False, True), (400, False, False),
                                    (1020007001, False, False)):
            with self.subTest(code=code), self.assertRaises(DataRefused) as caught:
                YudaoClient._data("synthetic", code, {"msg": "opaque"})
            self.assertEqual(caught.exception.permission_denied, denied)
            self.assertEqual(caught.exception.read_failed, failed)

    def test_operational_detail_failure_is_not_hidden_by_a_cached_list(self):
        data = YudaoGoalData(_Client(), DOCS)
        data.schema()
        with patch.object(data._client, "get", side_effect=DataRefused("offline", read_failed=True)), \
             patch.object(data, "_all") as listing:
            with self.assertRaises(DataRefused):
                data.read("crm/contract", "11", ("totalPrice",))
            listing.assert_not_called()

    def test_a_refused_read_carries_the_application_s_own_sentence(self):
        # The application's own words ("请选择时间范围") are what a person can act on; the
        # endpoint and the code stay in the message for the audit.
        from web_operation_agent_yudao.client import YudaoClient
        client = YudaoClient.for_token("http://yudao.invalid", "t1", "1")
        client._request = lambda *a, **k: (400, {"code": 400, "msg": "请求参数不正确:请选择时间范围"})
        with self.assertRaises(DataRefused) as caught:
            client.get("crm/statistics-product/get-product-sales-list")
        self.assertEqual(caught.exception.reason, "请求参数不正确:请选择时间范围")
        self.assertIn("crm/statistics-product/get-product-sales-list", str(caught.exception))

    def test_a_transport_failure_carries_no_application_sentence(self):
        import urllib.error
        from unittest.mock import patch
        from web_operation_agent_yudao.client import YudaoClient
        client = YudaoClient.for_token("http://yudao.invalid", "t1", "1")
        with patch("web_operation_agent_yudao.client.urllib.request.urlopen",
                   side_effect=urllib.error.URLError("no route to host")):
            with self.assertRaises(DataRefused) as caught:
                client.get("crm/customer/page")
        self.assertEqual(caught.exception.reason, "")
        self.assertIn("URLError", str(caught.exception))


class _StatisticsClient(_Client):
    """A list whose endpoint requires two conditions and reports them one at a time."""

    MENUS = [{"name": "ERP 系统", "path": "/erp", "children": [
        {"name": "销售统计", "path": "sale/statistics", "component": "erp/statistics/index"}]}]

    def get(self, path, **params):
        if path == "system/auth/get-permission-info":
            return {"menus": self.MENUS, "permissions": []}
        if path == "erp/sale/statistics/list":
            self.calls.append((path, params))
            missing = [name for name in ("deptId", "times") if params.get(name) is None]
            if missing:
                label = {"deptId": "部门 id", "times": "时间范围"}[missing[0]]
                said = f"请求参数不正确:{label}不能为空"
                raise DataRefused(f"{path}: 400 {said}", reason=said)
            return [{"id": 1, "amount": 10.0, "deptId": params["deptId"]}]
        return super().get(path, **params)


STATISTICS_DOCS = {
    "paths": {
        "/admin-api/erp/sale/statistics/list": {"get": {
            "tags": ["管理后台 - ERP 销售统计"],
            "parameters": [
                {"in": "query", "name": "deptId", "required": True, "description": "部门 id",
                 "schema": {"type": "integer"}},
                {"in": "query", "name": "times", "required": True, "description": "时间范围",
                 "schema": {"type": "integer"}}],
            "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/RStat"}}}}}}},
    },
    "components": {"schemas": {
        **_schemas("Stat", {"id": _prop("integer", "编号"), "amount": _prop("number", "金额"),
                            "deptId": _prop("integer", "部门编号")}),
    }},
}


class RefusalConditionTests(unittest.TestCase):
    def test_a_refused_read_names_every_condition_the_page_waits_for(self):
        # The application reports one missing parameter at a time; the person is told all
        # of them, as the documentation names them, and none is supplied.
        data = YudaoGoalData(_StatisticsClient(), STATISTICS_DOCS)
        data.schema()
        with self.assertRaises(DataRefused) as caught:
            data.rows("erp/sale/statistics", all_of())
        self.assertIn("部门 id", caught.exception.reason)
        self.assertIn("不能为空", caught.exception.reason)
        self.assertIn("时间范围", caught.exception.reason)
        self.assertIn("erp/sale/statistics/list: 400", str(caught.exception))

    def test_a_condition_the_application_already_named_is_not_repeated(self):
        client = _StatisticsClient()
        data = YudaoGoalData(client, STATISTICS_DOCS)
        data.schema()

        def answer(path, **params):
            raise DataRefused(f"{path}: 400 请求参数不正确:请选择时间范围",
                              reason="请求参数不正确:请选择时间范围")
        client.get = answer
        with self.assertRaises(DataRefused) as caught:
            data.rows("erp/sale/statistics", all_of())
        self.assertEqual(caught.exception.reason.count("时间范围"), 1)
        self.assertIn("部门 id", caught.exception.reason)


class ReferenceConventionTests(unittest.TestCase):
    """The deployment's own words, as the owner reviewed them."""

    def test_the_mall_s_words_for_an_unpaid_order_name_the_order_state(self):
        from web_operation_agent.goal.conventions import load_conventions
        from web_operation_agent_yudao.conventions import REFERENCE_CONVENTIONS

        conventions = load_conventions(REFERENCE_CONVENTIONS)
        unpaid = next(c for c in conventions if "还没付款" in c.terms)
        # The mall tracks payment as the order's state; the boolean field answers a
        # different question, and the convention says which one the words mean.
        self.assertEqual(unpaid.entity, "trade/order")
        self.assertEqual(unpaid.conditions, (("订单状态", ("待支付",)),))
        self.assertTrue(unpaid.named_in("商城订单里还没付款的有多少笔？"))
        self.assertFalse(unpaid.named_in("采购订单有几个？"))


class NativePageGoalTests(unittest.TestCase):
    def test_native_lists_and_records_require_mount_proof_without_changing_reads(self):
        from web_operation_agent_yudao.page_goals import NativePageGoalPort

        data = YudaoGoalData(_Client(), DOCS, ui=UiSource(
            records={"/crm/contract": "/crm/contract/detail/{id}"}))
        ordinary, native = DataGoalPort(data), NativePageGoalPort(data)
        for goal in (
            {"kind": "page", "entity": "crm/contract", "page": {"key": "/crm/contract"}},
            {"kind": "page", "entity": "crm/contract", "record": "云帆企业版合同"},
            {"kind": "fact", "entity": "crm/contract", "record": "云帆企业版合同", "fields": ["合同金额"]},
            {"kind": "page", "entity": "crm/contract", "record": "Missing synthetic contract"},
        ):
            with self.subTest(goal=goal):
                spec = GoalSpec.from_json({"deliverables": [goal]})
                before, after = ordinary.resolve(spec), native.resolve(spec)
                item = after.deliverables[0]
                if item.page:
                    self.assertTrue(item.page.requires_page_route)
                    self.assertTrue(item.page.expected_path)
                    from dataclasses import replace
                    self.assertEqual(before, replace(after, deliverables=(replace(item,
                        page=replace(item.page, requires_page_route=False)),)))
                else:
                    self.assertEqual(before, after)
