"""Reviewed relation, title, owner and unit boundaries of native records."""
import copy
import unittest

from web_operation_agent.business.errors import (BusinessToolReferenceError,
    BusinessToolRequestError, BusinessMutationRefusedError)
from .test_native_writes import Client, Data, context
from web_operation_agent_yudao.writes import native_tools


class Records(Client):
    def __init__(self):
        super().__init__()
        self.lists = {
            "erp/product-category/list": [
                {"id": 1, "name": "办公", "parentId": 0, "status": 0},
                {"id": 2, "name": "材料", "parentId": 1, "status": 0},
                {"id": 3, "name": "生产", "parentId": 0, "status": 0},
                {"id": 4, "name": "材料", "parentId": 3, "status": 0}],
            "erp/product-unit/simple-list": [{"id": 7, "name": "箱"}],
            "system/user/simple-list": [{"id": 9, "nickname": "合成员工", "deptName": "开发"}],
            "system/dept/simple-list": [{"id": 6, "name": "开发", "parentId": 0}],
            "product/category/list": [{"id": 8, "name": "办公", "parentId": 0, "status": 0}]}
        self.dictionary.append({"dictType": "system_notice_type", "label": "通知", "value": "1"})

    def get(self, path, **params):
        return copy.deepcopy(self.lists[path]) if path in self.lists else super().get(path, **params)


class RecordExpansionTests(unittest.TestCase):
    def setUp(self):
        self.client = Records()
        self.tools = native_tools(self.client, Data(self.client))

    def complete(self, operation, inputs):
        spec = self.tools.prepare_mutation(operation, inputs, context=context())
        spec.effect_projection.require(self.tools.get(operation).effect_contract.project_inputs(inputs))
        result = self.tools.execute_mutation(operation, spec.args, context=context())
        spec.effect_projection.require_receipt(result.content)
        return spec, result

    def product(self, category="办公/材料"):
        return {"name": "合成产品", "barCode": "001Ab", "category": category, "unit": "箱",
                "status": "开启", "salePrice": 19.9, "weight": 0.25, "remark": "保留"}

    def test_product_units_relations_and_minimal_patch_preserve_unrelated_values(self):
        spec, _ = self.complete("erp_product_create", self.product())
        self.assertEqual(spec.args["body"]["categoryId"], 2)
        self.assertEqual(spec.args["body"]["unitId"], 7)
        self.assertEqual(spec.args["body"]["salePrice"], 19.9)
        self.assertEqual(spec.args["body"]["weight"], 0.25)
        self.complete("erp_product_update", {"target": "合成产品", "changes": {"salePrice": 0}})
        row = self.client.records[("erp/product", 30)]
        self.assertEqual((row["barCode"], row["weight"], row["remark"]), ("001Ab", 0.25, "保留"))

    def test_ambiguous_hierarchy_and_changed_relation_fail_before_dispatch(self):
        with self.assertRaises(BusinessToolReferenceError) as caught:
            self.tools.prepare_mutation("erp_product_create", self.product("材料"), context=context())
        self.assertEqual(caught.exception.choices, ("办公/材料", "生产/材料"))
        spec = self.tools.prepare_mutation("erp_product_create", self.product(), context=context())
        self.client.lists["erp/product-category/list"][1]["parentId"] = 3
        with self.assertRaises(BusinessMutationRefusedError):
            self.tools.execute_mutation("erp_product_create", spec.args, context=context())
        self.assertFalse(self.client.writes)

    def test_clue_owner_is_explicit_resolved_and_preserved_on_update(self):
        spec, _ = self.complete("crm_clue_create", {"name": "合成线索", "owner": "合成员工（开发）"})
        self.assertEqual(spec.args["body"]["ownerUserId"], 9)
        self.complete("crm_clue_update", {"target": "合成线索", "changes": {"remark": "新备注"}})
        self.assertEqual(self.client.records[("crm/clue", 30)]["ownerUserId"], 9)
        with self.assertRaises(BusinessToolRequestError):
            self.tools.prepare_mutation("crm_clue_update", {"target": "合成线索", "changes": {"owner": "其他"}}, context=context())
        with self.assertRaises(BusinessToolRequestError):
            self.tools.prepare_mutation("crm_clue_create", {"name": "合成线索", "owner": "合成员工", "description": "应用未保存的字段"}, context=context())

    def test_notice_uses_title_and_full_body_without_pushing(self):
        inputs = {"title": "合成通知", "type": "通知", "content": "完整正文\n保留标点；", "status": "关闭"}
        spec, result = self.complete("system_notice_create", inputs)
        self.assertEqual(result.content["reference"], "合成通知（ID=30）")
        self.assertEqual(self.client.writes, [("POST", "system/notice/create", {
            **inputs, "type": 1, "status": 1})])

    def test_department_and_category_bind_explicit_root_or_existing_parent(self):
        for operation, inputs, parent in (
            ("system_dept_create", {"name": "合成部门", "parent": "开发", "leader": "合成员工",
                                   "sort": 0, "status": "开启"}, 6),
            ("product_category_create", {"name": "合成分类", "parent": "根节点", "picUrl": "https://example.test/p.png",
                                        "sort": 1, "status": "关闭"}, 0)):
            with self.subTest(operation=operation):
                spec, _ = self.complete(operation, inputs)
                self.assertEqual(spec.args["body"]["parentId"], parent)

    def test_duplicate_user_candidates_require_name_and_current_identity_together(self):
        self.client.lists["system/user/simple-list"].append({"id": 12, "nickname": "合成员工", "deptName": "开发"})
        with self.assertRaises(BusinessToolReferenceError):
            self.tools.prepare_mutation("crm_clue_create", {"name": "合成线索", "owner": "合成员工"}, context=context())
        spec = self.tools.prepare_mutation("crm_clue_create", {"name": "合成线索", "owner": "合成员工（ID=9）"}, context=context())
        self.assertEqual(spec.args["body"]["ownerUserId"], 9)
        for owner in ("其他姓名（ID=9）", "9"):
            with self.assertRaises(BusinessToolReferenceError):
                self.tools.prepare_mutation("crm_clue_create", {"name": "合成线索", "owner": owner}, context=context())

    def test_brand_update_preserves_required_picture_and_sort(self):
        self.complete("product_brand_create", {"name": "合成品牌", "picUrl": "https://example.test/p.png",
            "sort": 0, "status": "开启"})
        spec, _ = self.complete("product_brand_update", {"target": "合成品牌", "changes": {"description": "新描述"}})
        self.assertEqual(spec.args["body"]["picUrl"], "https://example.test/p.png")


if __name__ == "__main__":
    unittest.main()
