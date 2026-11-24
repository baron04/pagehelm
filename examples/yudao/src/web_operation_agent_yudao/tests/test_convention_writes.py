"""Unit tests for Yudao convention writes, ERP line totals, and disabled-field idempotency."""
from __future__ import annotations

import copy
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace as NS
import unittest

from web_operation_agent.business.errors import (
    BusinessMutationRefusedError,
    BusinessToolPermissionError,
    BusinessToolRequestError,
)
from web_operation_agent.schemas import ElementRef
from web_operation_agent.state.identity import OperationContext, PrincipalContext
from web_operation_agent_yudao.writes.convention_writes import YudaoConventionWritePort
from web_operation_agent_yudao.writes.native_submit import _enrich_erp_line_totals
from web_operation_agent_yudao.executor.slim_loop import _unavailable_already_matches
from web_operation_agent_yudao.rules.source import YudaoSource
from web_operation_agent_yudao.writes.writes import native_tools


def _ctx(actor_id: str = "9") -> OperationContext:
    now = int(time.time() * 1000)
    actor = PrincipalContext(
        1, "yudao", "1", "7", actor_id, "synthetic",
        now - 1000, now + 60000, ("yudao.read", "yudao.write"),
    )
    return OperationContext.create(actor, session_id="s", turn_id="t", operation_id="o", deadline_ms=now + 30000)


def _synthetic_openapi() -> dict:
    return {
        "paths": {
            "/admin-api/promotion/coupon-template/page": {
                "get": {
                    "tags": ["管理后台 - 优惠券模板"],
                    "parameters": [
                        {"name": "name", "in": "query", "schema": {"type": "string"}},
                        {"name": "pageNo", "in": "query", "schema": {"type": "integer"}},
                        {"name": "pageSize", "in": "query", "schema": {"type": "integer"}},
                    ],
                    "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CouponPage"}}}}},
                }
            },
            "/admin-api/promotion/coupon-template/get": {
                "get": {
                    "tags": ["管理后台 - 优惠券模板"],
                    "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CouponGet"}}}}},
                }
            },
            "/admin-api/promotion/coupon-template/update": {
                "put": {
                    "tags": ["管理后台 - 优惠券模板"],
                    "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/CouponUpdateReq"}}}},
                    "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultBoolean"}}}}},
                }
            },
            "/admin-api/promotion/coupon-template/update-status": {
                "put": {
                    "tags": ["管理后台 - 优惠券模板"],
                    "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/StatusReq"}}}},
                    "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultBoolean"}}}}},
                }
            },
            "/admin-api/hrm/employee/page": {
                "get": {
                    "tags": ["管理后台 - 员工档案"],
                    "parameters": [
                        {"name": "name", "in": "query", "schema": {"type": "string"}},
                        {"name": "pageNo", "in": "query", "schema": {"type": "integer"}},
                        {"name": "pageSize", "in": "query", "schema": {"type": "integer"}},
                    ],
                    "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/EmpPage"}}}}},
                }
            },
            "/admin-api/hrm/employee/get": {
                "get": {
                    "tags": ["管理后台 - 员工档案"],
                    "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/EmpGet"}}}}},
                }
            },
            "/admin-api/hrm/employee/update": {
                "put": {
                    "tags": ["管理后台 - 员工档案"],
                    "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/EmpUpdateReq"}}}},
                    "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultBoolean"}}}}},
                }
            },
            "/admin-api/hrm/salary/employee-info/get": {
                "get": {
                    "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/EmpGet"}}}}},
                }
            },
            "/admin-api/hrm/salary/employee-info/update": {
                "put": {
                    "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/EmpUpdateReq"}}}},
                    "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}},
                }
            },
            "/admin-api/erp/purchase-in/get": {
                "get": {
                    "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/ErpGet"}}}}},
                }
            },
            "/admin-api/erp/purchase-in/update": {
                "put": {
                    "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErpUpdateReq"}}}},
                    "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultBoolean"}}}}},
                }
            },
            "/admin-api/product/spu/page": {
                "get": {
                    "tags": ["管理后台 - 商品 SPU"],
                    "parameters": [
                        {"name": "name", "in": "query", "schema": {"type": "string"}},
                        {"name": "pageNo", "in": "query", "schema": {"type": "integer"}},
                        {"name": "pageSize", "in": "query", "schema": {"type": "integer"}},
                    ],
                    "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/SpuPage"}}}}},
                }
            },
            "/admin-api/product/spu/get-detail": {
                "get": {
                    "tags": ["管理后台 - 商品 SPU"],
                    "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/SpuGet"}}}}},
                }
            },
            "/admin-api/product/spu/update-status": {
                "put": {
                    "tags": ["管理后台 - 商品 SPU"],
                    "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/StatusReq"}}}},
                    "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultBoolean"}}}}},
                }
            },
        },
        "components": {
            "schemas": {
                "CommonResultBoolean": {"type": "object", "properties": {"code": {"type": "integer"}, "data": {"type": "boolean"}}},
                "CommonResultLong": {"type": "object", "properties": {"code": {"type": "integer"}, "data": {"type": "integer"}}},
                "StatusReq": {"type": "object", "required": ["id", "status"], "properties": {"id": {"type": "integer"}, "status": {"type": "integer"}}},
                "CouponUpdateReq": {
                    "type": "object",
                    "required": ["id", "name", "usePrice"],
                    "properties": {
                        "id": {"type": "integer"},
                        "name": {"type": "string", "description": "优惠券名称"},
                        "usePrice": {"type": "integer", "description": "满多少金额可用"},
                        "discountPrice": {"type": "integer", "description": "优惠金额"},
                        "discountPercent": {"type": "integer", "description": "折扣百分比"},
                        "description": {"type": "string", "description": "使用说明"},
                        "apiSecret": {"type": "string", "description": "通道密钥"},
                    },
                },
                "CouponResp": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "name": {"type": "string"},
                        "usePrice": {"type": "integer"},
                        "discountPrice": {"type": "integer"},
                        "discountPercent": {"type": "integer"},
                        "description": {"type": "string"},
                        "status": {"type": "integer"},
                    },
                },
                "CouponGet": {"type": "object", "properties": {"data": {"$ref": "#/components/schemas/CouponResp"}}},
                "CouponPage": {"type": "object", "properties": {"data": {"type": "object"}}},
                "EmpUpdateReq": {
                    "type": "object",
                    "required": ["id", "name", "mobile"],
                    "properties": {
                        "id": {"type": "integer"},
                        "name": {"type": "string", "description": "员工姓名"},
                        "mobile": {"type": "string", "description": "手机号"},
                        "deptId": {"type": "integer", "description": "部门编号"},
                        "status": {"type": "integer", "description": "员工状态"},
                        "entryTime": {"type": "string", "format": "date-time", "description": "入职时间"},
                        "remark": {"type": "string", "description": "备注"},
                    },
                },
                "EmpResp": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "name": {"type": "string"},
                        "mobile": {"type": "string"},
                        "deptId": {"type": "integer"},
                        "status": {"type": "integer"},
                        "entryTime": {"type": "integer"},
                        "remark": {"type": "string"},
                    },
                },
                "EmpGet": {"type": "object", "properties": {"data": {"$ref": "#/components/schemas/EmpResp"}}},
                "EmpPage": {"type": "object", "properties": {"data": {"type": "object"}}},
                "LineItem": {"type": "object", "properties": {"productId": {"type": "integer"}}},
                "ErpUpdateReq": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "items": {"type": "array", "items": {"$ref": "#/components/schemas/LineItem"}},
                    },
                },
                "ErpGet": {"type": "object", "properties": {"data": {"$ref": "#/components/schemas/ErpUpdateReq"}}},
                "SpuResp": {"type": "object", "properties": {"id": {"type": "integer"}, "name": {"type": "string"}, "status": {"type": "integer"}}},
                "SpuGet": {"type": "object", "properties": {"data": {"$ref": "#/components/schemas/SpuResp"}}},
                "SpuPage": {"type": "object", "properties": {"data": {"type": "object"}}},
            }
        },
    }


def _write_synthetic_release(root: Path) -> YudaoSource:
    (root / "server" / "yudao-module-hrm" / "src" / "main" / "java").mkdir(parents=True)
    (root / "ui" / "src" / "utils").mkdir(parents=True)
    (root / "ui" / "src" / "api" / "mall" / "promotion" / "coupon").mkdir(parents=True)
    (root / "ui" / "src" / "api" / "hrm").mkdir(parents=True)
    (root / "ui" / "src" / "views" / "mall" / "promotion" / "coupon").mkdir(parents=True)
    (root / "ui" / "src" / "views" / "hrm").mkdir(parents=True)

    (root / "ui" / "src" / "utils" / "dict.ts").write_text(
        "export enum DICT_TYPE {\n  HRM_EMPLOYEE_STATUS = 'hrm_employee_status',\n}\n",
        encoding="utf-8",
    )
    (root / "ui" / "src" / "api" / "mall" / "promotion" / "coupon" / "couponTemplate.ts").write_text(
        "export function updateCouponTemplate(data: any) {\n"
        "  return request.put({ url: '/promotion/coupon-template/update', data })\n"
        "}\n",
        encoding="utf-8",
    )
    (root / "ui" / "src" / "views" / "mall" / "promotion" / "coupon" / "CouponTemplateForm.vue").write_text(
        "<template><el-form :model=\"formData\">"
        "<el-form-item label=\"使用门槛\" prop=\"usePrice\"><el-input v-model=\"formData.usePrice\" /></el-form-item>"
        "<el-form-item label=\"优惠金额\" prop=\"discountPrice\"><el-input v-model=\"formData.discountPrice\" /></el-form-item>"
        "<el-form-item label=\"折扣\" prop=\"discountPercent\"><el-input v-model=\"formData.discountPercent\" /></el-form-item>"
        "</el-form></template>\n<script setup lang=\"ts\">\n"
        "import * as CouponTemplateApi from '@/api/mall/promotion/coupon/couponTemplate'\n"
        "const open = async (id: number) => {\n"
        "  const data = await CouponTemplateApi.getCouponTemplate(id)\n"
        "  formData.value = { ...data, discountPercent: data.discountPercent / 10.0 }\n"
        "}\n"
        "const submit = async () => {\n"
        "  const data = {\n"
        "    ...formData.value,\n"
        "    usePrice: convertToInteger(formData.value.usePrice),\n"
        "    discountPrice: convertToInteger(formData.value.discountPrice),\n"
        "    discountPercent: formData.value.discountPercent * 10,\n"
        "  }\n"
        "  await CouponTemplateApi.updateCouponTemplate(data)\n"
        "}\n</script>\n",
        encoding="utf-8",
    )
    (root / "ui" / "src" / "api" / "hrm" / "employee.ts").write_text(
        "export const updateEmployee = (data: any) => {\n"
        "  return request.put({ url: '/hrm/employee/update', data })\n"
        "}\n",
        encoding="utf-8",
    )
    (root / "ui" / "src" / "views" / "hrm" / "EmployeeForm.vue").write_text(
        "<template><el-form :model=\"formData\">"
        "<el-form-item label=\"员工状态\" prop=\"status\">"
        "<el-select v-model=\"formData.status\">"
        "<el-option v-for=\"d in getIntDictOptions(DICT_TYPE.HRM_EMPLOYEE_STATUS)\" :label=\"d.label\" :value=\"d.value\" />"
        "</el-select></el-form-item></el-form></template>\n<script setup lang=\"ts\">\n"
        "import * as EmployeeApi from '@/api/hrm/employee'\n"
        "const submit = async () => { await EmployeeApi.updateEmployee(formData.value) }\n"
        "</script>\n",
        encoding="utf-8",
    )
    return YudaoSource(root, release="v-synth")


class FakeYudaoClient:
    tenant = "1"

    def __init__(self):
        self.records = {
            ("promotion/coupon-template", 7): {
                "id": 7,
                "name": "合成满减券",
                "usePrice": 10000,
                "discountPrice": 1500,
                "discountPercent": None,
                "description": "原说明",
                "status": 0,
            },
            ("hrm/employee", 11): {
                "id": 11,
                "name": "周敏",
                "mobile": "13800000011",
                "deptId": 101,
                "status": 2,
                "entryTime": 1767225600000,
                "remark": "首批",
            },
            ("product/spu", 50): {
                "id": 50,
                "name": "合成台灯",
                "status": 1,
                "price": 9900,
            },
            ("product/spu", 51): {
                "id": 51,
                "name": "回收站商品",
                "status": -1,
                "price": 1000,
            },
        }
        self.writes: list[tuple] = []

    def get(self, path: str, **params):
        if path == "system/auth/get-permission-info":
            return {"user": {"id": 9, "nickname": "管理员"}, "roles": ["super_admin"]}
        if path == "system/dict-data/simple-list":
            return [
                {"dictType": "hrm_employee_status", "label": "正式", "value": "1"},
                {"dictType": "hrm_employee_status", "label": "试用", "value": "2"},
            ]
        if path == "system/dept/simple-list":
            return [
                {"id": 100, "name": "总公司", "parentId": 0},
                {"id": 101, "name": "人事部", "parentId": 100},
                {"id": 102, "name": "研发中心", "parentId": 100},
            ]
        if path.endswith("/page"):
            entity = path[:-len("/page")]
            name_q = (params.get("name") or "").strip().casefold()
            rows = [
                copy.deepcopy(r)
                for (ent, _), r in self.records.items()
                if ent == entity and (not name_q or name_q in str(r.get("name") or "").casefold())
            ]
            return {"total": len(rows), "list": rows}
        if path == "product/spu/get-detail":
            return copy.deepcopy(self.records.get(("product/spu", int(params["id"]))))
        if path.endswith("/get"):
            return copy.deepcopy(self.records.get((path[:-len("/get")], int(params["id"]))))
        raise AssertionError(f"unexpected GET {path} {params}")

    def write_once(self, method: str, path: str, *, body: dict, params=None, operation: str = ""):
        self.writes.append((method, path, copy.deepcopy(body), params, operation))
        if path.endswith("/update"):
            ent = path[:-len("/update")]
            self.records[(ent, body["id"])].update(body)
            return True
        if path.endswith("/update-status"):
            ent = path[:-len("/update-status")]
            rid = body["id"] if body else params["id"]
            st = body["status"] if body else params["status"]
            self.records[(ent, rid)]["status"] = st
            return True
        raise AssertionError(f"unexpected write {method} {path}")


class FakeGoalData:
    def __init__(self, docs: dict, source=None):
        self._documents = docs
        self._source = source
        self.invalidated: list[frozenset[str]] = []

    def time_zone(self) -> str:
        return "Asia/Shanghai"

    def find(self, entity: str, name: str, *, limit: int = 8):
        return ()

    def invalidate_reads(self, entities: frozenset[str]) -> None:
        self.invalidated.append(entities)


class YudaoConventionWritesTests(unittest.TestCase):
    def test_compiler_filters_non_flat_and_extracts_vue_scales_and_dicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = _write_synthetic_release(Path(tmp))
            data = FakeGoalData(_synthetic_openapi(), source=source)
            port = YudaoConventionWritePort(FakeYudaoClient(), data)
            updates = port.update_specs()
            statuses = port.status_specs()

            # Flat 1:1 entities included; CommonResultLong and object-array entities excluded
            self.assertIn("promotion/coupon-template", updates)
            self.assertIn("hrm/employee", updates)
            self.assertNotIn("hrm/salary/employee-info", updates)
            self.assertNotIn("erp/purchase-in", updates)

            coupon = updates["promotion/coupon-template"]
            # Secret field apiSecret stripped automatically
            self.assertIsNone(coupon.resolve_field("apiSecret"))
            self.assertEqual(coupon.resolve_field("使用门槛").unit_scale, 100)
            self.assertEqual(coupon.resolve_field("discountPrice").unit_scale, 100)
            self.assertEqual(coupon.resolve_field("discountPercent").unit_scale, 10)

            emp = updates["hrm/employee"]
            self.assertEqual(emp.resolve_field("status").enum_type, "hrm_employee_status")
            self.assertEqual(emp.resolve_field("deptId").reference_entity, "system/dept")
            self.assertEqual(emp.resolve_field("entryTime").date_kind, "epoch_ms")

            # promotion/coupon-template status is owned by dedicated promotion_coupon_template_status
            self.assertNotIn("promotion/coupon-template", statuses)
            self.assertIn("product/spu", statuses)

    def test_coupon_fen_update_refuses_unitless_number_and_preserves_other_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = _write_synthetic_release(Path(tmp))
            client = FakeYudaoClient()
            tools = native_tools(client, FakeGoalData(_synthetic_openapi(), source=source))

            # Unitless non-zero number on fen field is refused before any card
            with self.assertRaises(BusinessToolRequestError):
                tools.prepare_mutation(
                    "update_record",
                    {
                        "entity": "promotion/coupon-template",
                        "target": "合成满减券",
                        "changes": [{"field": "使用门槛", "value": 200}],
                    },
                    context=_ctx(),
                )

            # Explicit '200元' scales to 20000 fen and preserves discountPrice/description/status
            inputs = {
                "entity": "promotion/coupon-template",
                "target": "合成满减券",
                "changes": [
                    {"field": "使用门槛", "value": "200元"},
                    {"field": "description", "value": "原说明；补充限新客"},
                ],
            }
            spec = tools.prepare_mutation("update_record", inputs, context=_ctx())
            expected = tools.get("update_record").effect_contract.project_inputs(inputs)
            spec.effect_projection.require(expected)
            res = tools.execute_mutation("update_record", spec.args, context=_ctx())
            spec.effect_projection.require_receipt(res.content)
            self.assertTrue(res.content["verified"])
            self.assertEqual(client.records[("promotion/coupon-template", 7)]["usePrice"], 20000)
            self.assertEqual(client.records[("promotion/coupon-template", 7)]["discountPrice"], 1500)
            self.assertEqual(client.records[("promotion/coupon-template", 7)]["description"], "原说明；补充限新客")
            self.assertEqual(client.records[("promotion/coupon-template", 7)]["status"], 0)

    def test_employee_fk_and_dict_update_and_spu_two_state_toggle(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = _write_synthetic_release(Path(tmp))
            client = FakeYudaoClient()
            tools = native_tools(client, FakeGoalData(_synthetic_openapi(), source=source))

            emp_inputs = {
                "entity": "hrm/employee",
                "target": "周敏",
                "changes": [
                    {"field": "deptId", "value": "总公司/研发中心"},
                    {"field": "员工状态", "value": "正式"},
                ],
            }
            spec = tools.prepare_mutation("update_record", emp_inputs, context=_ctx())
            res = tools.execute_mutation("update_record", spec.args, context=_ctx())
            spec.effect_projection.require_receipt(res.content)
            self.assertTrue(res.content["verified"])
            self.assertEqual(client.records[("hrm/employee", 11)]["deptId"], 102)
            self.assertEqual(client.records[("hrm/employee", 11)]["status"], 1)
            self.assertEqual(client.records[("hrm/employee", 11)]["mobile"], "13800000011")

            # Product SPU two-state toggle: 上架 (1) -> 下架 (0)
            spu_inputs = {"entity": "product/spu", "target": "合成台灯", "status": "下架"}
            spu_spec = tools.prepare_mutation("toggle_record_status", spu_inputs, context=_ctx())
            spu_res = tools.execute_mutation("toggle_record_status", spu_spec.args, context=_ctx())
            spu_spec.effect_projection.require_receipt(spu_res.content)
            self.assertTrue(spu_res.content["verified"])
            self.assertEqual(client.records[("product/spu", 50)]["status"], 0)

            # Product SPU in recycle bin (-1) is refused as outside reversible two-state range
            with self.assertRaises(BusinessToolRequestError):
                tools.prepare_mutation(
                    "toggle_record_status",
                    {"entity": "product/spu", "target": "回收站商品", "status": "上架"},
                    context=_ctx(),
                )

            # Concurrent drift before dispatch is refused
            spu_up = {"entity": "product/spu", "target": "合成台灯", "status": "上架"}
            spec2 = tools.prepare_mutation("toggle_record_status", spu_up, context=_ctx())
            client.records[("product/spu", 50)]["price"] = 8800
            with self.assertRaises(BusinessMutationRefusedError):
                tools.execute_mutation("toggle_record_status", spec2.args, context=_ctx())

    def test_erp_line_totals_and_disabled_control_idempotency(self):
        raw = {
            "id": 99,
            "accountId": 998036,
            "items": [
                {"id": 1, "productPrice": 137.013, "count": 10, "taxPercent": 0, "taxPrice": 0},
                {"id": 2, "productPrice": 138.143, "count": 10, "taxPercent": 0, "taxPrice": 0},
            ],
        }
        enriched = _enrich_erp_line_totals("/admin-api/erp/purchase-in/", raw)
        self.assertEqual(enriched["items"][0]["totalProductPrice"], 1370.13)
        self.assertEqual(enriched["items"][0]["totalPrice"], 1370.13)
        self.assertEqual(enriched["items"][1]["totalPrice"], 1381.43)

        disabled_radio = ElementRef(
            ref="e5",
            role="radiogroup",
            name="记录类型",
            tag="div",
            epoch=1,
            attrs={"disabled": "true", "value": "调薪", "field_label": "记录类型"},
        )
        self.assertTrue(_unavailable_already_matches(disabled_radio, "调薪"))
        self.assertFalse(_unavailable_already_matches(disabled_radio, "定薪"))

    def test_java_update_semantics_excludes_conditional_cleanup_and_guards_immutable_and_nonnull(self):
        docs = _synthetic_openapi()
        docs["paths"]["/admin-api/mes/cal/plan/get"] = {
            "get": {"responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/EmpGet"}}}}}},
        }
        docs["paths"]["/admin-api/mes/cal/plan/update"] = {
            "put": {
                "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/EmpUpdateReq"}}}},
                "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultBoolean"}}}}},
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_synthetic_release(root)
            java_dir = root / "server" / "yudao-module-hrm" / "src" / "main" / "java" / "synth"
            java_dir.mkdir(parents=True)
            (java_dir / "ErrorCodeConstants.java").write_text(
                'package synth;\npublic interface ErrorCodeConstants {\n'
                '  ErrorCode EMP_LOCKED = new ErrorCode(1001, "已有锁定关联，禁止修改");\n}\n',
                encoding="utf-8",
            )
            (java_dir / "HrmEmployeeController.java").write_text(
                'package synth;\n@RequestMapping("/hrm/employee")\npublic class HrmEmployeeController {\n'
                '  private HrmEmployeeService employeeService;\n'
                '  @PutMapping("/update")\n  public CommonResult<Boolean> updateEmployee(EmpUpdateReq req) {\n'
                '    employeeService.updateEmployee(req);\n    return success(true);\n  }\n}\n',
                encoding="utf-8",
            )
            (java_dir / "HrmEmployeeServiceImpl.java").write_text(
                'package synth;\npublic class HrmEmployeeServiceImpl implements HrmEmployeeService {\n'
                '  private HrmEmployeeMapper employeeMapper;\n'
                '  public void updateEmployee(EmpUpdateReq updateReqVO) {\n'
                '    updateReqVO.setMobile(null);\n'
                '    HrmEmployeeDO oldEmp = employeeMapper.selectById(updateReqVO.getId());\n'
                '    if (Objects.nonNull(oldEmp.getLockId())) {\n'
                '      throw exception(EMP_LOCKED);\n    }\n'
                '    employeeMapper.updateById(BeanUtils.toBean(updateReqVO, HrmEmployeeDO.class));\n  }\n}\n',
                encoding="utf-8",
            )
            (java_dir / "MesCalPlanController.java").write_text(
                'package synth;\n@RequestMapping("/mes/cal/plan")\npublic class MesCalPlanController {\n'
                '  private MesCalPlanService planService;\n'
                '  @PutMapping("/update")\n  public CommonResult<Boolean> updatePlan(EmpUpdateReq req) {\n'
                '    planService.updatePlan(req);\n    return success(true);\n  }\n}\n',
                encoding="utf-8",
            )
            (java_dir / "MesCalPlanServiceImpl.java").write_text(
                'package synth;\npublic class MesCalPlanServiceImpl implements MesCalPlanService {\n'
                '  private MesCalPlanMapper planMapper;\n'
                '  public void updatePlan(EmpUpdateReq updateReqVO) {\n'
                '    if (updateReqVO.getStatus() == 1) {\n      updateReqVO.setRemark(null);\n    }\n'
                '    planMapper.updateWithShiftFieldCleanup(updateReqVO);\n  }\n}\n',
                encoding="utf-8",
            )
            source = YudaoSource(root, release="v-synth-java")
            client = FakeYudaoClient()
            port = YudaoConventionWritePort(client, FakeGoalData(docs, source=source))
            updates = port.update_specs()
            self.assertNotIn("mes/cal/plan", updates)
            self.assertIn("hrm/employee", updates)

            tools = native_tools(client, FakeGoalData(docs, source=source))
            # Unconditional setMobile(null) refuses editing mobile before showing any card
            with self.assertRaises(BusinessToolRequestError):
                tools.prepare_mutation(
                    "update_record",
                    {"entity": "hrm/employee", "target": "周敏", "changes": [{"field": "mobile", "value": "13900000000"}]},
                    context=_ctx(),
                )
            # Editing remark still preserves mobile in payload
            spec = tools.prepare_mutation(
                "update_record",
                {"entity": "hrm/employee", "target": "周敏", "changes": [{"field": "remark", "value": "二批"}]},
                context=_ctx(),
            )
            self.assertEqual(spec.args["payload"]["mobile"], "13800000011")

            # Objects.nonNull(oldEmp.getLockId()) refuses locked record before card
            client.records[("hrm/employee", 11)]["lockId"] = 777
            with self.assertRaises(BusinessToolRequestError):
                tools.prepare_mutation(
                    "update_record",
                    {"entity": "hrm/employee", "target": "周敏", "changes": [{"field": "remark", "value": "三批"}]},
                    context=_ctx(),
                )

    def test_provenance_scaling_append_secret_stripping_and_ui_root_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_synthetic_release(root)
            docs = _synthetic_openapi()
            # Add system/dept update & promotion/coupon-template update-status to verify dedicated exclusion
            docs["paths"]["/admin-api/system/dept/get"] = docs["paths"]["/admin-api/hrm/employee/get"]
            docs["paths"]["/admin-api/system/dept/update"] = docs["paths"]["/admin-api/hrm/employee/update"]

            # Also verify UiSource alone (source=None) still compiles labels/scales/dicts
            from web_operation_agent_yudao.rules.ui_source import UiSource
            ui_dir = root / "ui"
            (ui_dir / "src" / "router" / "modules").mkdir(parents=True)
            (ui_dir / "src" / "router" / "modules" / "remaining.ts").write_text("export default []\n", encoding="utf-8")
            ui_obj = UiSource.read(ui_dir)
            goal_data = FakeGoalData(docs, source=None)
            goal_data._ui = ui_obj

            client = FakeYudaoClient()
            # Seed hidden secret/credential fields on the target record
            client.records[("promotion/coupon-template", 7)]["apiSecret"] = "secret-value-123"
            client.records[("promotion/coupon-template", 7)]["aesKey"] = "aes-key-456"

            tools = native_tools(client, goal_data)
            update_def = tools.get("update_record")
            port = update_def.handler.port
            self.assertIn("promotion/coupon-template", port.update_specs())
            self.assertNotIn("system/dept", port.update_specs())
            self.assertNotIn("promotion/coupon-template", port.status_specs())
            self.assertEqual(port.dedicated_tool_for("system/dept", "update"), "system_dept_update")
            self.assertEqual(port.dedicated_tool_for("promotion/coupon-template", "status"), "promotion_coupon_template_status")

            # Secret fields stripped from EffectProjection and args without tripping _SENSITIVE_KEY ValueError
            pct_inputs = {
                "entity": "promotion/coupon-template",
                "target": "合成满减券",
                "changes": [{"field": "discountPercent", "value": "85%"}],
            }
            spec_pct = tools.prepare_mutation("update_record", pct_inputs, context=_ctx())
            self.assertNotIn("apiSecret", spec_pct.args["snapshot"])
            self.assertNotIn("aesKey", spec_pct.args["snapshot"])
            self.assertIn("restricted_field_digests", spec_pct.args)
            fake_proposal = NS(effect_projection=spec_pct.effect_projection)
            # "85%" accepted when user wrote "85%" or "8.5折"
            update_def.handler.validate_provenance(pct_inputs, ("把折扣改成85%",), fake_proposal)
            update_def.handler.validate_provenance(pct_inputs, ("把折扣改成8.5折",), fake_proposal)
            # 10x inflation ("85折" when user said "8.5折") refused
            bad_10x = {
                "entity": "promotion/coupon-template",
                "target": "合成满减券",
                "changes": [{"field": "discountPercent", "value": "85折"}],
            }
            spec_10x = tools.prepare_mutation("update_record", bad_10x, context=_ctx())
            with self.assertRaises(BusinessToolRequestError):
                update_def.handler.validate_provenance(bad_10x, ("把折扣改成8.5折",), NS(effect_projection=spec_10x.effect_projection))

            # 100x inflation ("25元" when user only wrote "25分") refused; "25元" for "2500分" accepted
            yuan_inputs = {
                "entity": "promotion/coupon-template",
                "target": "合成满减券",
                "changes": [{"field": "使用门槛", "value": "25元"}],
            }
            spec_yuan = tools.prepare_mutation("update_record", yuan_inputs, context=_ctx())
            with self.assertRaises(BusinessToolRequestError):
                update_def.handler.validate_provenance(yuan_inputs, ("满25分可用",), NS(effect_projection=spec_yuan.effect_projection))
            update_def.handler.validate_provenance(yuan_inputs, ("满2500分可用",), NS(effect_projection=spec_yuan.effect_projection))

            # Append preservation vs overwrite & None-clear refusal via SkillWrites._validate_prepared_update
            from web_operation_agent.goal.skill_writes import SkillWrites

            append_inputs = {
                "entity": "promotion/coupon-template",
                "target": "合成满减券",
                "changes": [{"field": "description", "value": "原说明；限新客"}],
            }
            spec_app = tools.prepare_mutation("update_record", append_inputs, context=_ctx())
            SkillWrites._validate_prepared_update(update_def, append_inputs, ("在说明中追加：限新客",), NS(effect_projection=spec_app.effect_projection))

            overwrite_inputs = {
                "entity": "promotion/coupon-template",
                "target": "合成满减券",
                "changes": [{"field": "description", "value": "限新客"}],
            }
            spec_ovr = tools.prepare_mutation("update_record", overwrite_inputs, context=_ctx())
            with self.assertRaises(BusinessToolRequestError):
                SkillWrites._validate_prepared_update(update_def, overwrite_inputs, ("在说明中追加：限新客",), NS(effect_projection=spec_ovr.effect_projection))

            clear_inputs = {
                "entity": "promotion/coupon-template",
                "target": "合成满减券",
                "changes": [{"field": "description", "value": None}],
            }
            spec_clr = tools.prepare_mutation("update_record", clear_inputs, context=_ctx())
            with self.assertRaises(BusinessToolRequestError):
                SkillWrites._validate_prepared_update(update_def, clear_inputs, ("在说明中追加：限新客",), NS(effect_projection=spec_clr.effect_projection))

            # Hidden secret drift detected safely before dispatch
            client.records[("promotion/coupon-template", 7)]["aesKey"] = "mutated-aes-key"
            with self.assertRaises(BusinessMutationRefusedError):
                tools.execute_mutation("update_record", spec_pct.args, context=_ctx())

    def test_optional_bounds_and_jsonformat_string_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_synthetic_release(Path(tmp))
            docs = _synthetic_openapi()
            # Add optional fixedEndTerm (minimum=1) and JsonFormat validStartTime (date-time)
            c_props = docs["components"]["schemas"]["CouponUpdateReq"]["properties"]
            c_props["fixedEndTerm"] = {"type": "integer", "description": "结束天数", "minimum": 1}
            c_props["validStartTime"] = {"type": "string", "format": "date-time", "description": "生效开始时间"}
            r_props = docs["components"]["schemas"]["CouponResp"]["properties"]
            r_props["fixedEndTerm"] = {"type": "integer"}
            r_props["validStartTime"] = {"type": "string"}

            from web_operation_agent_yudao.rules.ui_source import UiSource
            ui_dir = Path(tmp) / "ui"
            (ui_dir / "src" / "router" / "modules").mkdir(parents=True)
            (ui_dir / "src" / "router" / "modules" / "remaining.ts").write_text("export default []\n", encoding="utf-8")
            ui_obj = UiSource.read(ui_dir)
            goal_data = FakeGoalData(docs, source=None)
            goal_data._ui = ui_obj

            client = FakeYudaoClient()
            client.records[("promotion/coupon-template", 7)]["fixedEndTerm"] = 0
            client.records[("promotion/coupon-template", 7)]["validStartTime"] = "2026-07-10 10:00:00"

            # Simulate Yudao's asymmetric Jackson LocalDateTime deserializer:
            # GET emits @JsonFormat string ("YYYY-MM-DD HH:MM:SS"), while PUT calls getValueAsLong()
            # (valid epoch ms int -> formatted string; string -> "1970-01-01 08:00:00").
            orig_write_once = client.write_once

            def jackson_write_once(method: str, path: str, *, body: dict, params=None, operation: str = ""):
                body = dict(body)
                if path == "promotion/coupon-template/update" and "validStartTime" in body:
                    vst = body["validStartTime"]
                    if type(vst) is int:
                        from datetime import datetime
                        from zoneinfo import ZoneInfo
                        body["validStartTime"] = datetime.fromtimestamp(vst / 1000.0, tz=ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
                    elif isinstance(vst, str):
                        body["validStartTime"] = "1970-01-01 08:00:00"
                return orig_write_once(method, path, body=body, params=params, operation=operation)

            client.write_once = jackson_write_once

            tools = native_tools(client, goal_data)

            # Updating fixedEndTerm explicitly to 0 fails schema bound validation before card
            with self.assertRaises(BusinessToolRequestError):
                tools.prepare_mutation(
                    "update_record",
                    {
                        "entity": "promotion/coupon-template",
                        "target": "合成满减券",
                        "changes": [{"field": "fixedEndTerm", "value": 0}],
                    },
                    context=_ctx(),
                )

            # Updating usePrice omits untouched fixedEndTerm=0 and encodes validStartTime as int ms,
            # preserving "2026-07-10 10:00:00" cleanly on asymmetric readback.
            spec_up = tools.prepare_mutation(
                "update_record",
                {
                    "entity": "promotion/coupon-template",
                    "target": "合成满减券",
                    "changes": [{"field": "使用门槛", "value": "200元"}],
                },
                context=_ctx(),
            )
            self.assertNotIn("fixedEndTerm", spec_up.args["payload"])
            self.assertEqual(spec_up.args["snapshot"]["fixedEndTerm"], 0)
            self.assertIs(type(spec_up.args["payload"]["validStartTime"]), int)
            res = tools.execute_mutation("update_record", spec_up.args, context=_ctx())
            self.assertTrue(res.content["verified"])
            self.assertEqual(client.records[("promotion/coupon-template", 7)]["usePrice"], 20000)
            self.assertEqual(client.records[("promotion/coupon-template", 7)]["fixedEndTerm"], 0)
            self.assertEqual(client.records[("promotion/coupon-template", 7)]["validStartTime"], "2026-07-10 10:00:00")

            # Explicit update of @JsonFormat date-time field sends epoch ms int and matches readback string
            spec_dt = tools.prepare_mutation(
                "update_record",
                {
                    "entity": "promotion/coupon-template",
                    "target": "合成满减券",
                    "changes": [{"field": "validStartTime", "value": "2026-07-11 10:00:00"}],
                },
                context=_ctx(),
            )
            self.assertIs(type(spec_dt.args["payload"]["validStartTime"]), int)
            res_dt = tools.execute_mutation("update_record", spec_dt.args, context=_ctx())
            self.assertTrue(res_dt.content["verified"])
            self.assertEqual(client.records[("promotion/coupon-template", 7)]["validStartTime"], "2026-07-11 10:00:00")

    def test_validate_provenance_handles_candidate_dataclasses_and_existing_values(self):
        from web_operation_agent.goal.skill_writes import SkillWrites

        with tempfile.TemporaryDirectory() as tmp:
            _write_synthetic_release(Path(tmp))
            docs = copy.deepcopy(_synthetic_openapi())
            docs["components"]["schemas"]["CouponUpdateReq"]["properties"]["fixedEndTerm"] = {
                "type": "integer",
                "description": "结束天数",
            }
            docs["components"]["schemas"]["CouponResp"]["properties"]["fixedEndTerm"] = {"type": "integer"}
            goal_data = FakeGoalData(docs, source=None)
            client = FakeYudaoClient()
            client.records[("promotion/coupon-template", 7)]["fixedEndTerm"] = 7
            tools = native_tools(client, goal_data)
            update_def = tools.get("update_record")

            # 1. unit_scale == 1 integer field sourced from existing_values keyed by f_spec.name
            int_inputs = {
                "entity": "promotion/coupon-template",
                "target": "合成满减券",
                "changes": [{"field": "fixedEndTerm", "value": 12}],
            }
            spec_int = tools.prepare_mutation("update_record", int_inputs, context=_ctx())
            SkillWrites._validate_prepared_update(
                update_def,
                int_inputs,
                ("把结束天数设为参照值",),
                NS(effect_projection=spec_int.effect_projection),
                existing_values={"fixedEndTerm": ("12",), "usePrice": ("999",)},
            )

            # 2. date_kind field sourced from existing_values keyed by f_spec.name
            dt_inputs = {
                "entity": "hrm/employee",
                "target": "周敏",
                "changes": [{"field": "入职时间", "value": "2026-10-20 09:00:00"}],
            }
            spec_dt = tools.prepare_mutation("update_record", dt_inputs, context=_ctx())
            update_def.handler.validate_provenance(
                dt_inputs,
                ("把周敏入职时间改为参考值",),
                NS(effect_projection=spec_dt.effect_projection),
                existing_values={"entryTime": ("2026-10-20 09:00:00",), "mobile": ("13800000011",)},
            )

            # 3. Ungrounded numeric or mismatched candidate value still fails closed
            bad_int = {
                "entity": "promotion/coupon-template",
                "target": "合成满减券",
                "changes": [{"field": "fixedEndTerm", "value": 99}],
            }
            spec_bad = tools.prepare_mutation("update_record", bad_int, context=_ctx())
            with self.assertRaises(BusinessToolRequestError):
                update_def.handler.validate_provenance(
                    bad_int,
                    ("把结束天数改成参考值",),
                    NS(effect_projection=spec_bad.effect_projection),
                    existing_values={"fixedEndTerm": ("12",), "usePrice": ("99",)},
                )

            # 4. Append preservation works with existing_values passed explicitly
            app_inputs = {
                "entity": "promotion/coupon-template",
                "target": "合成满减券",
                "changes": [{"field": "description", "value": "原说明；补充一条"}],
            }
            spec_app = tools.prepare_mutation("update_record", app_inputs, context=_ctx())
            update_def.handler.validate_provenance(
                app_inputs,
                ("在说明后补充：补充一条",),
                NS(effect_projection=spec_app.effect_projection),
                existing_values={"description": ("原说明",), "usePrice": ("100",)},
            )

    def test_step13_compiled_create_defaults_sentinels_jobnumber_and_password_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_synthetic_release(root)
            java_base = root / "server" / "yudao-module-synth" / "src" / "main" / "java" / "pkg"
            (java_base / "vo").mkdir(parents=True)
            (java_base / "ctrl").mkdir(parents=True)
            (java_base / "svc").mkdir(parents=True)
            (java_base / "enums").mkdir(parents=True)
            (java_base / "enums" / "HrmEmployeeIdTypeEnum.java").write_text(
                'package pkg.enums;\npublic enum HrmEmployeeIdTypeEnum {\n  ID_CARD(1, "身份证");\n  private final Integer type; private final String name;\n'
                '  HrmEmployeeIdTypeEnum(Integer type, String name) { this.type = type; this.name = name; }\n'
                '  public Integer getType() { return type; } public String getName() { return name; }\n}\n',
                encoding="utf-8",
            )
            (java_base / "vo" / "CrmReceivableSaveReqVO.java").write_text(
                "package pkg.vo;\npublic class CrmReceivableSaveReqVO {\n"
                "  @NotNull private Long contractId;\n"
                "  @NotNull private Long ownerUserId;\n"
                "  @NotNull private java.math.BigDecimal price;\n"
                "  @NotNull private java.time.LocalDateTime returnTime;\n}\n",
                encoding="utf-8",
            )
            (java_base / "ctrl" / "CrmReceivableController.java").write_text(
                'package pkg.ctrl;\nimport pkg.vo.*;\n@RequestMapping("/crm/receivable")\npublic class CrmReceivableController {\n'
                '  private CrmReceivableService receivableService;\n'
                '  @PostMapping("/create") public CommonResult<Long> create(@RequestBody CrmReceivableSaveReqVO req) { return success(receivableService.createReceivable(req)); }\n}\n',
                encoding="utf-8",
            )
            (java_base / "svc" / "CrmReceivableServiceImpl.java").write_text(
                "package pkg.svc;\npublic class CrmReceivableServiceImpl implements CrmReceivableService {\n"
                "  private CrmReceivablePlanService receivablePlanService;\n"
                "  public Long createReceivable(CrmReceivableSaveReqVO req) { receivablePlanService.updateReceivablePlanReceivableId(1L, 2L); return 2L; }\n}\n",
                encoding="utf-8",
            )
            (java_base / "vo" / "HrmEmployeeSaveReqVO.java").write_text(
                "package pkg.vo;\npublic class HrmEmployeeSaveReqVO {\n"
                "  @NotBlank private String name;\n"
                "  private String jobNumber;\n"
                "  private String country;\n"
                "  @InEnum(HrmEmployeeIdTypeEnum.class) private Integer idType;\n"
                "  @NotNull private Integer entryStatus;\n"
                "  @NotNull private Integer type;\n"
                "  @NotNull private Integer probation;\n"
                "  @NotNull private Long deptId;\n}\n",
                encoding="utf-8",
            )
            (java_base / "ctrl" / "HrmEmployeeController.java").write_text(
                'package pkg.ctrl;\nimport pkg.vo.*;\n@RequestMapping("/hrm/employee")\npublic class HrmEmployeeController {\n'
                '  private HrmEmployeeService employeeService;\n'
                '  @PostMapping("/create") public CommonResult<Long> create(@RequestBody HrmEmployeeSaveReqVO req) { return success(employeeService.createEmployee(req)); }\n}\n',
                encoding="utf-8",
            )
            (java_base / "svc" / "HrmEmployeeServiceImpl.java").write_text(
                "package pkg.svc;\npublic class HrmEmployeeServiceImpl implements HrmEmployeeService {\n"
                "  public Long createEmployee(HrmEmployeeSaveReqVO req) { sendEmployeeOpenedMessage(req); return 1L; }\n"
                "  private void sendEmployeeOpenedMessage(Object e) { executeAfterCommit(() -> sendEmployeeMessage()); }\n"
                "  private void executeAfterCommit(Runnable r) { r.run(); }\n"
                "  private void sendEmployeeMessage() { notifyMessageSendApi.sendSingleMessageToAdmin(null); }\n}\n",
                encoding="utf-8",
            )
            (java_base / "vo" / "UserSaveReqVO.java").write_text(
                "package pkg.vo;\npublic class UserSaveReqVO {\n"
                "  private Long id;\n  @NotBlank private String username;\n  @NotBlank private String nickname;\n  private String password;\n"
                "  @AssertTrue public boolean isPasswordValid() { return id != null || password != null; }\n}\n",
                encoding="utf-8",
            )
            (java_base / "ctrl" / "UserController.java").write_text(
                'package pkg.ctrl;\nimport pkg.vo.*;\n@RequestMapping("/system/user")\npublic class UserController {\n'
                '  private AdminUserService userService;\n'
                '  @PostMapping("/create") public CommonResult<Long> create(@RequestBody UserSaveReqVO req) { return success(userService.createUser(req)); }\n'
                '  @PutMapping("/update") public CommonResult<Boolean> update(@RequestBody UserSaveReqVO req) { userService.updateUser(req); return success(true); }\n}\n',
                encoding="utf-8",
            )
            (java_base / "svc" / "AdminUserServiceImpl.java").write_text(
                "package pkg.svc;\npublic class AdminUserServiceImpl implements AdminUserService {\n"
                "  private AdminUserMapper userMapper;\n"
                "  public Long createUser(UserSaveReqVO req) { return 1L; }\n"
                "  public void updateUser(UserSaveReqVO req) { userMapper.updateById(null); }\n}\n",
                encoding="utf-8",
            )

            ui_api = root / "ui" / "src" / "api"
            (ui_api / "crm" / "receivable").mkdir(parents=True, exist_ok=True)
            (ui_api / "crm" / "receivable" / "index.ts").write_text(
                "export const getReceivable = async (id: number) => {\n  return await request.get({ url: '/crm/receivable/get?id=' + id })\n}\n"
                "export const createReceivable = async (data: any) => {\n  return await request.post({ url: '/crm/receivable/create', data })\n}\n",
                encoding="utf-8",
            )
            (ui_api / "hrm" / "employee").mkdir(parents=True, exist_ok=True)
            (ui_api / "hrm" / "employee" / "index.ts").write_text(
                "export const createEmployee = async (data: any) => { return await request.post<number>({ url: '/hrm/employee/create', data }) }\n",
                encoding="utf-8",
            )
            ui_views = root / "ui" / "src" / "views"
            (ui_views / "crm" / "receivable").mkdir(parents=True, exist_ok=True)
            (ui_views / "crm" / "receivable" / "ReceivableForm.vue").write_text(
                '<script setup>\nimport * as ReceivableApi from "@/api/crm/receivable"\n'
                'const open = () => { formData.value.ownerUserId = getCurrentUserId() }\n'
                'const submit = async () => { await ReceivableApi.createReceivable(formData.value) }\n</script>\n',
                encoding="utf-8",
            )
            (ui_views / "hrm" / "employee").mkdir(parents=True, exist_ok=True)
            (ui_views / "hrm" / "employee" / "EmployeeForm.vue").write_text(
                '<script setup>\nimport * as EmployeeApi from "@/api/hrm/employee"\n'
                'function createDefaultFormData() {\n  return {\n    name: "",\n    country: "中国",\n    idType: 1,\n    entryStatus: 1,\n    type: 1,\n    probation: 0,\n    deptId: 0\n  }\n}\n'
                'const submit = async () => { await EmployeeApi.createEmployee(formData.value) }\n</script>\n',
                encoding="utf-8",
            )

            docs = {
                "paths": {
                    "/admin-api/crm/contract/page": {"get": {"parameters": [], "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/EmpPage"}}}}}}},
                    "/admin-api/crm/receivable/get": {"get": {"responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/RecvGet"}}}}}}},
                    "/admin-api/crm/receivable/create": {"post": {"tags": ["管理后台 - CRM 回款"], "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/RecvCreate"}}}}, "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}}}},
                    "/admin-api/hrm/employee/get": {"get": {"responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/HrmEmpGet"}}}}}}},
                    "/admin-api/hrm/employee/create": {"post": {"tags": ["管理后台 - 员工档案"], "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/HrmEmpCreate"}}}}, "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}}}},
                    "/admin-api/system/user/page": {"get": {"parameters": [], "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/EmpPage"}}}}}}},
                    "/admin-api/system/user/get": {"get": {"responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/UserGet"}}}}}}},
                    "/admin-api/system/user/create": {"post": {"tags": ["管理后台 - 用户"], "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/UserReq"}}}}, "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}}}},
                    "/admin-api/system/user/update": {"put": {"tags": ["管理后台 - 用户"], "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/UserReq"}}}}, "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultBoolean"}}}}}}},
                },
                "components": {"schemas": {
                    "CommonResultLong": {"type": "object", "properties": {"code": {"type": "integer"}, "data": {"type": "integer"}}},
                    "CommonResultBoolean": {"type": "object", "properties": {"code": {"type": "integer"}, "data": {"type": "boolean"}}},
                    "EmpPage": {"type": "object", "properties": {"data": {"type": "object", "properties": {"list": {"type": "array", "items": {"type": "object"}}}}}},
                    "RecvCreate": {"type": "object", "required": ["contractId", "ownerUserId", "price", "returnTime"], "properties": {
                        "contractId": {"type": "integer", "description": "合同"}, "ownerUserId": {"type": "integer", "description": "负责人"},
                        "price": {"type": "number", "description": "回款金额"}, "returnTime": {"type": "string", "format": "date-time", "description": "回款日期"},
                    }},
                    "RecvGet": {"type": "object", "properties": {"data": {"type": "object", "properties": {
                        "id": {"type": "integer"}, "no": {"type": "string"}, "customerId": {"type": "integer"},
                        "contractId": {"type": "integer"}, "ownerUserId": {"type": "integer"}, "price": {"type": "number"}, "returnTime": {"type": "integer"},
                    }}}},
                    "HrmEmpCreate": {"type": "object", "required": ["name", "entryStatus", "type", "probation"], "properties": {
                        "name": {"type": "string", "description": "姓名"}, "jobNumber": {"type": "string", "description": "工号"},
                        "country": {"type": "string", "description": "国家"}, "idType": {"type": "integer", "description": "证件类型"},
                        "entryStatus": {"type": "integer", "description": "入职状态"}, "type": {"type": "integer", "description": "聘用形式"},
                        "probation": {"type": "integer", "description": "试用期"}, "deptId": {"type": "integer", "description": "部门"},
                    }},
                    "HrmEmpGet": {"type": "object", "properties": {"data": {"type": "object", "properties": {
                        "id": {"type": "integer"}, "name": {"type": "string"}, "jobNumber": {"type": "string"},
                        "country": {"type": "string"}, "idType": {"type": "integer"}, "entryStatus": {"type": "integer"},
                        "type": {"type": "integer"}, "probation": {"type": "integer"}, "deptId": {"type": "integer"}, "status": {"type": "integer"},
                    }}}},
                    "UserReq": {"type": "object", "required": ["username", "nickname"], "properties": {
                        "id": {"type": "integer"}, "username": {"type": "string", "description": "用户名"},
                        "nickname": {"type": "string", "description": "昵称"}, "password": {"type": "string", "description": "密码"},
                    }},
                    "UserGet": {"type": "object", "properties": {"data": {"type": "object", "properties": {
                        "id": {"type": "integer"}, "username": {"type": "string"}, "nickname": {"type": "string"},
                    }}}},
                }},
            }

            class Client(FakeYudaoClient):
                def __init__(self):
                    super().__init__()
                    self.next_id = 500
                    self.records[("crm/contract", 30)] = {"id": 30, "name": "战略合同A", "customerId": 88}
                    self.records[("system/user", 40)] = {"id": 40, "username": "u40", "nickname": "原昵称"}

                def get(self, path: str, **params):
                    if path == "crm/contract/page":
                        return {"total": 1, "list": [copy.deepcopy(self.records[("crm/contract", 30)])]}
                    if path == "crm/permission/list":
                        return [{"userId": 9, "level": 1}]
                    return super().get(path, **params)

                def write_once(self, method: str, path: str, *, body: dict, params=None, operation: str = "", expect_result: str = ""):
                    if path.endswith("/create"):
                        ent = path[:-len("/create")]
                        self.next_id += 1
                        rec = {"id": self.next_id, **copy.deepcopy(body)}
                        if ent == "crm/receivable":
                            rec["customerId"] = 88
                            rec["no"] = f"HK-{self.next_id}"
                        if ent == "hrm/employee":
                            rec["status"] = 1
                        self.records[(ent, self.next_id)] = rec
                        return self.next_id
                    return super().write_once(method, path, body=body, params=params, operation=operation)

            source = YudaoSource(root, release="v-step13-create")
            client = Client()
            tools = native_tools(client, FakeGoalData(docs, source=source))
            create_def = tools.get("create_record")
            update_def = tools.get("update_record")
            port = create_def.handler.port

            # Password create excluded; password update stripped
            self.assertNotIn("system/user", port.create_specs())
            self.assertIn("必填密码", port.excluded_create_reason("system/user"))
            self.assertIsNone(port.update_specs()["system/user"].resolve_field("password"))
            with self.assertRaises(BusinessToolRequestError):
                tools.prepare_mutation("create_record", {"entity": "system/user", "fields": [{"field": "username", "value": "u1"}]}, context=_ctx())

            # D1 actor default on crm/receivable + cross-record copy block
            recv_in = {
                "entity": "crm/receivable",
                "fields": [
                    {"field": "contractId", "value": "战略合同A"},
                    {"field": "price", "value": 5000},
                    {"field": "returnTime", "value": "2026-11-20 10:00:00"},
                ],
            }
            spec_recv = tools.prepare_mutation("create_record", recv_in, context=_ctx())
            self.assertTrue(any("管理员（ID=9）（页面默认值）" in (getattr(d, "value", "") or str(d)) for d in spec_recv.details))
            with self.assertRaises(BusinessToolRequestError):
                create_def.handler.validate_provenance(
                    recv_in, ("按参考合同创建回款",), NS(effect_projection=spec_recv.effect_projection), existing_values={"price": ("5000",)},
                )
            res_recv = tools.execute_mutation("create_record", spec_recv.args, context=_ctx())
            self.assertTrue(res_recv.content["verified"])
            self.assertEqual(client.records[("crm/receivable", 501)]["ownerUserId"], 9)

            # D2/D3 sentinel deptId=0 refused + conditional jobNumber required when entryStatus==1
            with self.assertRaises(BusinessToolRequestError):
                tools.prepare_mutation("create_record", {"entity": "hrm/employee", "fields": [{"field": "name", "value": "新员工"}]}, context=_ctx())
            emp_spec = tools.prepare_mutation(
                "create_record",
                {"entity": "hrm/employee", "fields": [{"field": "name", "value": "新员工"}, {"field": "jobNumber", "value": "E-100"}, {"field": "deptId", "value": "人事部"}]},
                context=_ctx(),
            )
            emp_res = tools.execute_mutation("create_record", emp_spec.args, context=_ctx())
            self.assertTrue(emp_res.content["verified"])
            self.assertEqual(client.records[("hrm/employee", 502)]["country"], "中国")
            self.assertEqual(client.records[("hrm/employee", 502)]["idType"], 1)

            # User update strips optional password & succeeds
            u_spec = tools.prepare_mutation(
                "update_record",
                {"entity": "system/user", "target": "u40", "changes": [{"field": "nickname", "value": "新昵称"}]},
                context=_ctx(),
            )
            u_res = tools.execute_mutation("update_record", u_spec.args, context=_ctx())
            self.assertTrue(u_res.content["verified"])
            self.assertNotIn("password", u_spec.args["payload"])

    def test_step13_update_for_edit_aggregates_composite_salary_statuses_and_relations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_synthetic_release(root)
            java_base = root / "server" / "yudao-module-agg" / "src" / "main" / "java" / "pkg"
            for sub in ("vo", "ctrl", "svc"):
                (java_base / sub).mkdir(parents=True, exist_ok=True)
            (java_base / "vo" / "CandidateVO.java").write_text("package pkg.vo;\npublic class CandidateSaveReqVO { private Long id; private String name; private Integer education; }\n", encoding="utf-8")
            (java_base / "ctrl" / "CandidateController.java").write_text(
                'package pkg.ctrl;\nimport pkg.vo.*;\n@RequestMapping("/hrm/recruit/candidate")\npublic class CandidateController {\n'
                '  private HrmRecruitCandidateService candService;\n'
                '  @PutMapping("/update") public CommonResult<Boolean> upd(@RequestBody CandidateSaveReqVO req) { candService.updateCandidate(req); return success(true); }\n}\n',
                encoding="utf-8",
            )
            (java_base / "svc" / "HrmRecruitCandidateServiceImpl.java").write_text(
                "package pkg.svc;\npublic class HrmRecruitCandidateServiceImpl implements HrmRecruitCandidateService {\n"
                "  private CandidateMapper candidateMapper;\n"
                "  public void updateCandidate(CandidateSaveReqVO req) { candidateMapper.updateForEdit(req); }\n}\n",
                encoding="utf-8",
            )
            (java_base / "vo" / "ErpPOVO.java").write_text(
                "package pkg.vo;\npublic class ErpPurchaseOrderSaveReqVO {\n  private Long id; private Long accountId; private java.util.List<Item> items;\n"
                "  public static class Item { private Long id; private Long productId; private java.math.BigDecimal count; private String remark; }\n}\n"
                "public class ErpPurchaseOrderRespVO {\n  private Long id; private Long accountId; private java.util.List<Item> items;\n"
                "  public static class Item { private Long id; private Long productId; private java.math.BigDecimal count; private String remark; }\n}\n",
                encoding="utf-8",
            )
            (java_base / "ctrl" / "ErpPOController.java").write_text(
                'package pkg.ctrl;\nimport pkg.vo.*;\n@RequestMapping("/erp/purchase-order")\npublic class ErpPOController {\n'
                '  private ErpPurchaseOrderService poService;\n'
                '  @GetMapping("/get") public CommonResult<ErpPurchaseOrderRespVO> get(Long id) { return null; }\n'
                '  @PutMapping("/update") public CommonResult<Boolean> upd(@RequestBody ErpPurchaseOrderSaveReqVO req) { poService.updatePurchaseOrder(req); return success(true); }\n}\n',
                encoding="utf-8",
            )
            (java_base / "svc" / "ErpPurchaseOrderServiceImpl.java").write_text(
                "package pkg.svc;\npublic class ErpPurchaseOrderServiceImpl implements ErpPurchaseOrderService {\n"
                "  private PoItemMapper poItemMapper;\n"
                "  public void updatePurchaseOrder(ErpPurchaseOrderSaveReqVO req) { poItemMapper.updateBatch(null); }\n}\n",
                encoding="utf-8",
            )

            docs = {
                "paths": {
                    "/admin-api/hrm/recruit/candidate/page": {"get": {"parameters": [], "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/ListPage"}}}}}}},
                    "/admin-api/hrm/recruit/candidate/get": {"get": {"responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CandGet"}}}}}}},
                    "/admin-api/hrm/recruit/candidate/update": {"put": {"tags": ["管理后台 - 候选人"], "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/CandReq"}}}}, "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultBoolean"}}}}}}},
                    "/admin-api/erp/purchase-order/page": {"get": {"parameters": [], "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/ListPage"}}}}}}},
                    "/admin-api/erp/purchase-order/get": {"get": {"responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/POGet"}}}}}}},
                    "/admin-api/erp/purchase-order/update": {"put": {"tags": ["管理后台 - 采购订单"], "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/POReq"}}}}, "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultBoolean"}}}}}}},
                    "/admin-api/fms/config/account-set/simple-list": {"get": {"responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/ListPage"}}}}}}},
                    "/admin-api/fms/voucher/page": {"get": {"parameters": [{"name": "accountSetId", "in": "query", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/ListPage"}}}}}}},
                    "/admin-api/fms/voucher/get": {"get": {"parameters": [{"name": "accountSetId", "in": "query", "required": True, "schema": {"type": "integer"}}, {"name": "id", "in": "query", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/VoucherGet"}}}}}}},
                    "/admin-api/fms/voucher/update": {"put": {"tags": ["管理后台 - 会计凭证"], "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/VoucherReq"}}}}, "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultBoolean"}}}}}}},
                    "/admin-api/hrm/salary/employee-info/page": {"get": {"parameters": [], "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/ListPage"}}}}}}},
                    "/admin-api/hrm/salary/employee-info/get": {"get": {"parameters": [{"name": "employeeId", "in": "query", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/SalGet"}}}}}}},
                    "/admin-api/hrm/salary/employee-info/update": {"put": {"tags": ["管理后台 - 员工薪资"], "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/SalReq"}}}}, "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}}}},
                    "/admin-api/mes/dv/check-plan/page": {"get": {"parameters": [], "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/ListPage"}}}}}}},
                    "/admin-api/mes/dv/check-plan/get": {"get": {"responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/PlanGet"}}}}}}},
                    "/admin-api/mes/dv/check-plan/enable": {"put": {"tags": ["管理后台 - 点检计划"], "parameters": [{"name": "id", "in": "query", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultBoolean"}}}}}}},
                    "/admin-api/mes/dv/check-plan/disable": {"put": {"tags": ["管理后台 - 点检计划"], "parameters": [{"name": "id", "in": "query", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultBoolean"}}}}}}},
                    "/admin-api/erp/account/page": {"get": {"parameters": [], "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/ListPage"}}}}}}},
                    "/admin-api/erp/account/get": {"get": {"responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/AcctGet"}}}}}}},
                    "/admin-api/erp/account/update-default-status": {"put": {"tags": ["管理后台 - 结算账户"], "parameters": [{"name": "id", "in": "query", "required": True, "schema": {"type": "integer"}}, {"name": "defaultStatus", "in": "query", "required": True, "schema": {"type": "boolean"}}], "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultBoolean"}}}}}}},
                    "/admin-api/system/user/page": {"get": {"parameters": [], "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/ListPage"}}}}}}},
                    "/admin-api/system/user/get": {"get": {"responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CandGet"}}}}}}},
                    "/admin-api/system/role/simple-list": {"get": {"responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/ListPage"}}}}}}},
                    "/admin-api/system/permission/assign-user-role": {"post": {"tags": ["管理后台 - 权限"], "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/AssignRoleReq"}}}}, "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultBoolean"}}}}}}},
                    "/admin-api/system/permission/list-user-roles": {"get": {"parameters": [{"name": "userId", "in": "query", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/SetLong"}}}}}}},
                },
                "components": {"schemas": {
                    "CommonResultBoolean": {"type": "object", "properties": {"code": {"type": "integer"}, "data": {"type": "boolean"}}},
                    "CommonResultLong": {"type": "object", "properties": {"code": {"type": "integer"}, "data": {"type": "integer"}}},
                    "ListPage": {"type": "object", "properties": {"data": {"type": "object", "properties": {"list": {"type": "array", "items": {"type": "object"}}}}}},
                    "SetLong": {"type": "object", "properties": {"code": {"type": "integer"}, "data": {"type": "array", "items": {"type": "integer"}}}},
                    "CandReq": {"type": "object", "required": ["id", "name"], "properties": {"id": {"type": "integer"}, "name": {"type": "string"}, "education": {"type": "integer"}}},
                    "CandGet": {"type": "object", "properties": {"data": {"type": "object", "properties": {"id": {"type": "integer"}, "name": {"type": "string"}, "nickname": {"type": "string"}, "education": {"type": "integer"}}}}},
                    "POItem": {"type": "object", "properties": {"id": {"type": "integer"}, "productId": {"type": "integer"}, "count": {"type": "number"}, "remark": {"type": "string"}}},
                    "POReq": {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}, "accountId": {"type": "integer"}, "items": {"type": "array", "items": {"$ref": "#/components/schemas/POItem"}}}},
                    "POGet": {"type": "object", "properties": {"data": {"type": "object", "properties": {"id": {"type": "integer"}, "no": {"type": "string"}, "accountId": {"type": "integer"}, "totalPrice": {"type": "number"}, "items": {"type": "array", "items": {"$ref": "#/components/schemas/POItem"}}}}}},
                    "Aux": {"type": "object", "properties": {"id": {"type": "integer"}, "typeId": {"type": "integer"}, "itemId": {"type": "integer"}}},
                    "VEntry": {"type": "object", "properties": {"id": {"type": "integer"}, "digest": {"type": "string"}, "debitAmount": {"type": "number"}, "auxiliaries": {"type": "array", "items": {"$ref": "#/components/schemas/Aux"}}}},
                    "VoucherReq": {"type": "object", "required": ["id", "accountSetId"], "properties": {"id": {"type": "integer"}, "accountSetId": {"type": "integer"}, "entries": {"type": "array", "items": {"$ref": "#/components/schemas/VEntry"}}}},
                    "VoucherGet": {"type": "object", "properties": {"data": {"type": "object", "properties": {"id": {"type": "integer"}, "accountSetId": {"type": "integer"}, "voucherWordName": {"type": "string"}, "voucherNumber": {"type": "integer"}, "entries": {"type": "array", "items": {"$ref": "#/components/schemas/VEntry"}}}}}},
                    "OptItem": {"type": "object", "properties": {"code": {"type": "integer"}, "name": {"type": "string"}, "value": {"type": "number"}}},
                    "SalReq": {"type": "object", "required": ["employeeId", "recordType", "changeReason"], "properties": {"employeeId": {"type": "integer"}, "recordType": {"type": "integer"}, "changeReason": {"type": "integer"}, "salaryOptions": {"type": "array", "items": {"$ref": "#/components/schemas/OptItem"}}}},
                    "SalGet": {"type": "object", "properties": {"data": {"type": "object", "properties": {"id": {"type": "integer"}, "employeeId": {"type": "integer"}, "employeeName": {"type": "string"}, "changeReason": {"type": "integer"}, "regularSalary": {"type": "number"}, "salaryOptions": {"type": "array", "items": {"$ref": "#/components/schemas/OptItem"}}}}}},
                    "PlanGet": {"type": "object", "properties": {"data": {"type": "object", "properties": {"id": {"type": "integer"}, "name": {"type": "string"}, "status": {"type": "integer"}}}}},
                    "AcctGet": {"type": "object", "properties": {"data": {"type": "object", "properties": {"id": {"type": "integer"}, "name": {"type": "string"}, "defaultStatus": {"type": "boolean"}}}}},
                    "AssignRoleReq": {"type": "object", "properties": {"userId": {"type": "integer"}, "roleIds": {"type": "array", "items": {"type": "integer"}}}},
                }},
            }

            class AggClient(FakeYudaoClient):
                def fenced_read_only(self, _writing_gets=()):
                    from contextlib import nullcontext
                    return nullcontext()

                def __init__(self):
                    super().__init__()
                    self.user_roles = {80: [1]}
                    self.change_records = {}
                    self.records[("hrm/recruit/candidate", 60)] = {"id": 60, "name": "常予宸", "education": 2}
                    self.records[("erp/purchase-order", 61)] = {
                        "id": 61, "no": "PO-61", "accountId": 99, "totalPrice": 300,
                        "items": [{"id": 1, "productId": 10, "count": 2, "remark": "A"}, {"id": 2, "productId": 20, "count": 1, "remark": "B"}],
                    }
                    self.records[("fms/voucher", 62)] = {
                        "id": 62, "accountSetId": 5, "voucherWordName": "记", "voucherNumber": 1,
                        "entries": [
                            {"id": 11, "digest": "差旅", "debitAmount": 100, "auxiliaries": [{"id": 101, "typeId": 1, "itemId": 7}]},
                            {"id": 12, "digest": "银行", "debitAmount": 0, "auxiliaries": []},
                        ],
                    }
                    self.records[("hrm/salary/employee-info", 63)] = {
                        "id": 900, "employeeId": 63, "employeeName": "于振宁", "changeReason": 2, "regularSalary": 8000,
                        "salaryOptions": [{"code": 10101, "name": "基本工资", "value": 8000}],
                    }
                    self.records[("mes/dv/check-plan", 64)] = {"id": 64, "name": "月度点检", "status": 1}
                    self.records[("erp/account", 65)] = {"id": 65, "name": "工行主账户", "defaultStatus": False}
                    self.records[("system/user", 80)] = {"id": 80, "name": "李明", "nickname": "李明"}

                def get(self, path: str, **params):
                    if path == "fms/config/account-set/simple-list":
                        return [{"id": 5, "name": "主账套"}]
                    if path == "fms/voucher/get":
                        assert params.get("accountSetId") == 5
                        return copy.deepcopy(self.records[("fms/voucher", int(params["id"]))])
                    if path == "hrm/salary/employee-info/get":
                        return copy.deepcopy(self.records[("hrm/salary/employee-info", int(params["employeeId"]))])
                    if path == "hrm/salary/change-record/get":
                        return copy.deepcopy(self.change_records[int(params["id"])])
                    if path == "system/role/simple-list":
                        return [{"id": 1, "name": "普通员工"}, {"id": 2, "name": "财务主管"}]
                    if path == "system/permission/list-user-roles":
                        return list(self.user_roles[int(params["userId"])])
                    return super().get(path, **params)

                def write_once(self, method: str, path: str, *, body: dict, params=None, operation: str = "", expect_result: str = ""):
                    if path == "hrm/salary/employee-info/update":
                        self.writes.append((method, path, copy.deepcopy(body), params, operation))
                        self.change_records[701] = {"id": 701, "employeeId": body["employeeId"], "salaryOptions": copy.deepcopy(body["salaryOptions"])}
                        return 701
                    if path == "mes/dv/check-plan/disable":
                        self.records[("mes/dv/check-plan", int(params["id"]))]["status"] = 0
                        return True
                    if path == "erp/account/update-default-status":
                        self.records[("erp/account", int(params["id"]))]["defaultStatus"] = params["defaultStatus"]
                        return True
                    if path == "system/permission/assign-user-role":
                        self.user_roles[int(body["userId"])] = list(body["roleIds"])
                        return True
                    if path == "erp/purchase-order/update":
                        rec = self.records[("erp/purchase-order", body["id"])]
                        rec.update(copy.deepcopy(body))
                        rec["totalPrice"] = sum(float(i["count"]) * 100 for i in rec["items"])
                        return True
                    if path == "fms/voucher/update":
                        assert body.get("accountSetId") == 5
                        self.records[("fms/voucher", body["id"])].update(copy.deepcopy(body))
                        return True
                    return super().write_once(method, path, body=body, params=params, operation=operation)

            source = YudaoSource(root, release="v-step13-agg")
            client = AggClient()
            tools = native_tools(client, FakeGoalData(docs, source=source))

            # Cat 2 updateForEdit candidate
            c_spec = tools.prepare_mutation("update_record", {"entity": "hrm/recruit/candidate", "target": "常予宸", "changes": [{"field": "education", "value": 4}]}, context=_ctx())
            self.assertTrue(tools.execute_mutation("update_record", c_spec.args, context=_ctx()).content["verified"])
            self.assertEqual(client.records[("hrm/recruit/candidate", 60)]["education"], 4)

            # Cat 3 ERP PO child item update preserving untouched accountId & line #2
            po_spec = tools.prepare_mutation(
                "update_record",
                {"entity": "erp/purchase-order", "target": "PO-61", "line_changes": [{"collection": "items", "match": "#1", "changes": [{"field": "count", "value": 5}]}]},
                context=_ctx(),
            )
            self.assertTrue(tools.execute_mutation("update_record", po_spec.args, context=_ctx()).content["verified"])
            self.assertEqual(client.records[("erp/purchase-order", 61)]["accountId"], 99)
            self.assertEqual(client.records[("erp/purchase-order", 61)]["items"][1]["remark"], "B")

            # Cat 3 FMS voucher composite (accountSetId, id) + 2-level auxiliaries preserved
            fv_spec = tools.prepare_mutation(
                "update_record",
                {"entity": "fms/voucher", "target": "记-1", "line_changes": [{"collection": "entries", "match": "差旅", "changes": [{"field": "debitAmount", "value": 250}]}]},
                context=_ctx(),
            )
            self.assertTrue(tools.execute_mutation("update_record", fv_spec.args, context=_ctx()).content["verified"])
            self.assertEqual(client.records[("fms/voucher", 62)]["entries"][0]["auxiliaries"][0]["itemId"], 7)

            # Cat 3 Salary change-record readback
            sal_spec = tools.prepare_mutation(
                "update_record",
                {"entity": "hrm/salary/employee-info", "target": "于振宁", "line_changes": [{"collection": "salaryOptions", "match": "基本工资", "changes": [{"field": "value", "value": 9500}]}]},
                context=_ctx(),
            )
            sal_res = tools.execute_mutation("update_record", sal_spec.args, context=_ctx())
            self.assertTrue(sal_res.content["verified"])
            self.assertEqual(client.change_records[701]["salaryOptions"][0]["value"], 9500)

            # Cat 4 split disable, boolean query toggle, and ERP status redirect
            dis_spec = tools.prepare_mutation("toggle_record_status", {"entity": "mes/dv/check-plan", "target": "月度点检", "status": "关闭"}, context=_ctx())
            self.assertTrue(tools.execute_mutation("toggle_record_status", dis_spec.args, context=_ctx()).content["verified"])
            self.assertEqual(client.records[("mes/dv/check-plan", 64)]["status"], 0)
            acc_spec = tools.prepare_mutation("toggle_record_status", {"entity": "erp/account", "target": "工行主账户", "status": "开启"}, context=_ctx())
            self.assertTrue(tools.execute_mutation("toggle_record_status", acc_spec.args, context=_ctx()).content["verified"])
            self.assertTrue(client.records[("erp/account", 65)]["defaultStatus"])
            with self.assertRaises(BusinessToolRequestError) as erp_exc:
                tools.prepare_mutation("toggle_record_status", {"entity": "erp/purchase-order", "target": "PO-61", "status": "关闭"}, context=_ctx())
            self.assertIn("erp_document_status", str(erp_exc.exception))

            # Cat 4b relation assignment + reconciler checks
            rel_spec = tools.prepare_mutation(
                "assign_record_relations",
                {"entity": "system/user", "target": "李明", "relation": "roleIds", "items": ["财务主管"]},
                context=_ctx(),
            )
            self.assertTrue(tools.execute_mutation("assign_record_relations", rel_spec.args, context=_ctx()).content["verified"])
            self.assertEqual(client.user_roles[80], [2])

            from web_operation_agent_yudao.host.reconciliation import YudaoMutationReconciler
            reconciler = YudaoMutationReconciler(
                client,
                NS(validate=lambda *a, **kw: None),
                supported_tools=frozenset({"create_record", "assign_record_relations"}),
            )
            self.assertEqual(
                reconciler.reconcile(
                    "assign_record_relations",
                    {"entity": "system/user", "id": 80, "read_path": "system/permission/list-user-roles", "id_param": "userId", "expected": {"roleIds": [2]}},
                    evidence={},
                    principal=_ctx().principal,
                ).outcome,
                "completed",
            )
            self.assertEqual(
                reconciler.reconcile(
                    "create_record",
                    {"entity": "hrm/recruit/candidate", "id": 9999, "expected": {"name": "不存在"}},
                    evidence={},
                    principal=_ctx().principal,
                ).outcome,
                "failed",
            )

    def test_concurrent_warm_never_corrupts_compiled_write_ast(self):
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_synthetic_release(root)
            docs = {
                "paths": {
                    "/admin-api/crm/receivable/get": {"get": {"responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/RGet"}}}}}}},
                    "/admin-api/crm/receivable/create": {"post": {"tags": ["管理后台 - 回款"], "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/RCreate"}}}}, "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}}}},
                },
                "components": {"schemas": {
                    "CommonResultLong": {"type": "object", "properties": {"code": {"type": "integer"}, "data": {"type": "integer"}}},
                    "RCreate": {"type": "object", "required": ["price"], "properties": {"price": {"type": "number", "description": "金额"}}},
                    "RGet": {"type": "object", "properties": {"data": {"type": "object", "properties": {"id": {"type": "integer"}, "price": {"type": "number"}}}}},
                }},
            }
            source = YudaoSource(root, release="v-warm-race-1")
            ui_root = root / "ui" / "src"
            (ui_root / "views" / "crm" / "receivable").mkdir(parents=True, exist_ok=True)
            t = threading.Thread(target=source.warm)
            t.start()
            port = YudaoConventionWritePort(FakeYudaoClient(), FakeGoalData(docs, source=source))
            creates = port.create_specs()
            t.join(timeout=10)
            self.assertIn("crm/receivable", creates)

    def test_framework_enum_versioned_update_do_drop_and_relation_required_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_synthetic_release(root)
            fw = root / "server" / "yudao-framework" / "yudao-common" / "src" / "main" / "java" / "pkg"
            fw.mkdir(parents=True, exist_ok=True)
            (fw / "CommonStatusEnum.java").write_text(
                'package pkg;\npublic enum CommonStatusEnum implements ArrayValuable<Integer> {\n'
                '  ENABLE(0, "开启"), DISABLE(1, "关闭");\n}\n',
                encoding="utf-8",
            )
            mod = root / "server" / "yudao-module-agg" / "src" / "main" / "java" / "pkg"
            for sub in ("dal/dataobject", "controller/vo", "controller", "service"):
                (mod / sub).mkdir(parents=True, exist_ok=True)
            (mod / "dal" / "dataobject" / "ProdCatDO.java").write_text(
                "package pkg;\npublic class ProdCatDO extends BaseDO { private Long id; private String name; }\n",
                encoding="utf-8",
            )
            (mod / "controller" / "vo" / "CatSaveReqVO.java").write_text(
                "package pkg;\npublic class CatSaveReqVO { private Long id; private String name; private String description; }\n",
                encoding="utf-8",
            )
            (mod / "controller" / "CatController.java").write_text(
                'package pkg;\n@RequestMapping("/product/category")\npublic class CatController {\n'
                '  private ProdCatService prodCatService;\n'
                '  @PutMapping("/update") public CommonResult<Boolean> u(@RequestBody CatSaveReqVO req) { prodCatService.updateCat(req); return success(true); }\n}\n',
                encoding="utf-8",
            )
            (mod / "service" / "ProdCatServiceImpl.java").write_text(
                "package pkg;\npublic class ProdCatServiceImpl implements ProdCatService {\n"
                "  private ProdCatMapper catMapper;\n"
                "  public void updateCat(CatSaveReqVO req) { ProdCatDO d = BeanUtils.toBean(req, ProdCatDO.class); catMapper.updateById(d); }\n}\n",
                encoding="utf-8",
            )
            (mod / "controller" / "vo" / "TmplReqVO.java").write_text(
                "package pkg;\npublic class TmplReqVO { private Long id; private String name; private java.util.List<Item> items;\n"
                "  public static class Item { private String name; }\n}\n",
                encoding="utf-8",
            )
            (mod / "controller" / "TmplController.java").write_text(
                'package pkg;\n@RequestMapping("/hrm/performance/assessment-template")\npublic class TmplController {\n'
                '  private TmplService tmplService;\n'
                '  @PutMapping("/update") public CommonResult<Boolean> u(@RequestBody TmplReqVO req) { tmplService.updateTmpl(req); return success(true); }\n}\n',
                encoding="utf-8",
            )
            (mod / "service" / "TmplServiceImpl.java").write_text(
                "package pkg;\npublic class TmplServiceImpl implements TmplService {\n"
                "  private TmplMapper tmplMapper;\n"
                "  public void updateTmpl(TmplReqVO req) { TmplDO d = BeanUtils.toBean(req, TmplDO.class).setId(null); tmplMapper.insert(d); }\n}\n",
                encoding="utf-8",
            )

            source = YudaoSource(root, release="v-step14-filters")
            self.assertEqual(source.enum_value_labels().get("CommonStatusEnum"), {"0": "开启", "1": "关闭"})
            docs = {
                "paths": {
                    "/admin-api/product/category/get": {"get": {"responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CatGet"}}}}}}},
                    "/admin-api/product/category/update": {"put": {"tags": ["分类"], "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/CatReq"}}}}, "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultBoolean"}}}}}}},
                    "/admin-api/hrm/performance/assessment-template/get": {"get": {"responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/TmplGet"}}}}}}},
                    "/admin-api/hrm/performance/assessment-template/update": {"put": {"tags": ["考核"], "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/TmplSchema"}}}}, "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultBoolean"}}}}}}},
                    "/admin-api/member/user/update-level": {"put": {"tags": ["会员"], "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/LvlReq"}}}}, "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultBoolean"}}}}}}},
                },
                "components": {"schemas": {
                    "CommonResultBoolean": {"type": "object", "properties": {"code": {"type": "integer"}, "data": {"type": "boolean"}}},
                    "CatReq": {"type": "object", "required": ["id", "name"], "properties": {"id": {"type": "integer"}, "name": {"type": "string"}, "description": {"type": "string"}}},
                    "CatGet": {"type": "object", "properties": {"data": {"type": "object", "properties": {"id": {"type": "integer"}, "name": {"type": "string"}, "description": {"type": "string"}}}}},
                    "TmplItem": {"type": "object", "properties": {"name": {"type": "string"}}},
                    "TmplSchema": {"type": "object", "required": ["id", "name"], "properties": {"id": {"type": "integer"}, "name": {"type": "string"}, "items": {"type": "array", "items": {"$ref": "#/components/schemas/TmplItem"}}}},
                    "TmplGet": {"type": "object", "properties": {"data": {"type": "object", "properties": {"id": {"type": "integer"}, "name": {"type": "string"}, "items": {"type": "array", "items": {"$ref": "#/components/schemas/TmplItem"}}}}}},
                    "LvlReq": {"type": "object", "required": ["id", "reason"], "properties": {"id": {"type": "integer"}, "levelId": {"type": "integer"}, "reason": {"type": "string"}}},
                }},
            }
            port = YudaoConventionWritePort(FakeYudaoClient(), FakeGoalData(docs, source=source))
            cat_spec = port.update_specs()["product/category"]
            self.assertIsNotNone(cat_spec.resolve_field("name"))
            self.assertIsNone(cat_spec.resolve_field("description"))
            self.assertNotIn("hrm/performance/assessment-template", port.aggregate_update_specs())
            self.assertNotIn("member/user", port.relation_specs())

    def test_fms_composite_readback_oa_file_node_pms_search_and_kb_group_refusal(self):
        class ExtraClient:
            tenant = "1"

            def __init__(self):
                self.calls = []
                self.aux = {10: {"id": 10, "name": "差旅项目", "accountSetId": 7, "auxiliaryTypeId": 3, "status": 0}}
                self.doc_shares = {88: {"id": 5, "documentId": 88, "shareUserIds": [1]}}

            def get(self, path: str, **params):
                self.calls.append((path, dict(params)))
                if path == "system/auth/get-permission-info":
                    return {"user": {"id": 1, "nickname": "管理员"}, "roles": []}
                if path == "fms/config/account-set/simple-list":
                    return [{"id": 7, "name": "主账套"}]
                if path == "fms/config/auxiliary-type/simple-list":
                    assert params.get("accountSetId") == 7
                    return [{"id": 3, "name": "项目"}]
                if path == "fms/config/auxiliary-item/page":
                    assert params.get("accountSetId") == 7 and params.get("auxiliaryTypeId") == 3
                    return {"list": [dict(self.aux[10])]}
                if path == "fms/config/auxiliary-item/get":
                    assert params.get("accountSetId") == 7 and int(params["id"]) == 10
                    return dict(self.aux[10])
                if path == "oa/file-node/page":
                    assert params.get("scope") == "my"
                    return {"list": [{"id": 31, "name": "研发目录", "parentId": 0}]}
                if path == "oa/file-node/get":
                    return {"id": 31, "name": "研发目录", "parentId": 0}
                if path == "oa/file-node/directory-list":
                    return [{"id": 30, "name": "根目录", "parentId": 0}, {"id": 31, "name": "研发目录", "parentId": 0}]
                if path == "pms/kb/document/search-page":
                    return {"list": [{"id": 88, "title": "交付规范"}]}
                if path == "pms/kb/document-share/get":
                    return dict(self.doc_shares[int(params["documentId"])])
                raise AssertionError(f"unexpected GET {path} {params}")

        docs = {
            "paths": {
                "/admin-api/fms/config/account-set/simple-list": {"get": {}},
                "/admin-api/fms/config/auxiliary-type/simple-list": {"get": {}},
                "/admin-api/fms/config/auxiliary-item/page": {
                    "get": {
                        "parameters": [
                            {"name": "accountSetId", "in": "query", "required": True},
                            {"name": "auxiliaryTypeId", "in": "query", "required": True},
                            {"name": "pageNo", "in": "query", "required": True},
                            {"name": "pageSize", "in": "query", "required": True},
                        ]
                    }
                },
                "/admin-api/fms/config/auxiliary-item/get": {
                    "get": {
                        "parameters": [
                            {"name": "accountSetId", "in": "query", "required": True},
                            {"name": "id", "in": "query", "required": True},
                        ],
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/AuxGet"}}}}},
                    }
                },
                "/admin-api/fms/config/auxiliary-item/create": {
                    "post": {
                        "tags": ["辅助核算"],
                        "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/AuxCreate"}}}},
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}},
                    }
                },
                "/admin-api/oa/file-node/page": {
                    "get": {
                        "parameters": [
                            {"name": "scope", "in": "query", "required": True},
                            {"name": "pageNo", "in": "query", "required": True},
                            {"name": "pageSize", "in": "query", "required": True},
                        ]
                    }
                },
                "/admin-api/oa/file-node/get": {"get": {}},
                "/admin-api/oa/file-node/directory-list": {"get": {}},
                "/admin-api/oa/file-node/update-parent": {
                    "put": {
                        "requestBody": {"content": {"application/json": {"schema": {"properties": {"id": {"type": "integer"}, "parentId": {"type": "integer"}}}}}}
                    }
                },
                "/admin-api/pms/kb/document/search-page": {"get": {}},
                "/admin-api/pms/kb/document-share/get": {"get": {}},
                "/admin-api/pms/kb/document-share/update-member-list": {
                    "put": {
                        "requestBody": {"content": {"application/json": {"schema": {"properties": {"documentId": {"type": "integer"}, "shareUserIds": {"type": "array", "items": {"type": "integer"}}}}}}}
                    }
                },
            },
            "components": {"schemas": {
                "CommonResultLong": {"type": "object", "properties": {"code": {"type": "integer"}, "data": {"type": "integer"}}},
                "AuxCreate": {"type": "object", "properties": {"accountSetId": {"type": "integer"}, "auxiliaryTypeId": {"type": "integer"}, "name": {"type": "string"}}},
                "AuxGet": {"type": "object", "properties": {"data": {"type": "object", "properties": {"id": {"type": "integer"}, "accountSetId": {"type": "integer"}, "auxiliaryTypeId": {"type": "integer"}, "name": {"type": "string"}}}}},
            }},
        }
        client = ExtraClient()
        port = YudaoConventionWritePort(client, FakeGoalData(docs))
        aux_create = port.create_specs()["fms/config/auxiliary-item"]
        # 1. resolve_target via accountSetId + auxiliaryTypeId, then bare read_record hits cached composite_hint
        resolved_aux = port.resolve_target("fms/config/auxiliary-item", "差旅项目（ID=10）", spec=aux_create)
        self.assertEqual(resolved_aux["accountSetId"], 7)
        reread_aux = port.read_record("fms/config/auxiliary-item", 10)
        self.assertEqual(reread_aux["name"], "差旅项目")
        created_aux = port.read_created_record(aux_create, 10, {"accountSetId": 7, "name": "差旅项目"})
        self.assertEqual(created_aux["accountSetId"], 7)

        # 2. oa/file-node resolve_target (scope="my") & ref list (directory-list)
        file_rel = port.relation_specs()["oa/file-node"][0]
        resolved_node = port.resolve_target("oa/file-node", "研发目录（ID=31）", spec=file_rel)
        self.assertEqual(resolved_node["id"], 31)
        lp, _nk, dir_rows, _qp = port._list_ref_rows("oa/file-node")
        self.assertEqual(lp, "oa/file-node/directory-list")
        self.assertEqual([r["id"] for r in dir_rows], [30, 31])

        # 3. pms/kb/document-share resolve_target via search-page
        share_rel = port.relation_specs()["pms/kb/document-share"][0]
        resolved_doc = port.resolve_target("pms/kb/document-share", "交付规范（ID=88）", spec=share_rel)
        self.assertEqual(resolved_doc["documentId"], 88)

        # 4. pms/kb/group refuses both type=1 (ALL) and type=2 (UNGROUPED), allows type=3 (CUSTOM)
        with self.assertRaises(BusinessToolRequestError):
            port.check_record_updatable("pms/kb/group", {"id": 1, "type": 1})
        with self.assertRaises(BusinessToolRequestError):
            port.check_record_updatable("pms/kb/group", {"id": 2, "type": 2})
        port.check_record_updatable("pms/kb/group", {"id": 3, "type": 3})

    def test_step14_compiler_volatiles_workflow_guards_uuid_and_list_read_fallback(self):
        class RegClient:
            tenant = "1"

            def get(self, path: str, **params):
                if path == "hrm/salary/option/list":
                    return [{"id": 99, "name": "岗位补贴", "enabled": True}]
                if path == "bpm/model/list":
                    return [{"id": "3342b203-ce3a-11f1-b09f-2a95d9691e80", "name": "离职审批", "deploymentState": 1}]
                if path == "bpm/model/get":
                    return {"id": "3342b203-ce3a-11f1-b09f-2a95d9691e80", "name": "离职审批", "deploymentState": 1}
                if path == "product/spu/get-detail":
                    return {"id": 23, "name": "便携扩展坞", "price": 1000, "skus": [{"id": 23, "name": None, "price": 1000}]}
                if path == "ai/knowledge/document/page":
                    return {"list": [{"id": 77, "name": "研发手册"}]}
                if path == "ai/knowledge/segment/page" and params.get("documentId") == 77:
                    return {"list": [{"id": 701, "documentId": 77, "content": "分段A", "status": 0}]}
                if path == "ai/knowledge/segment/get" and str(params.get("id")) == "701":
                    return {"id": 701, "documentId": 77, "content": "分段A", "status": 0}
                raise AssertionError(f"unexpected GET {path} {params}")

        def _ok_resp(ref_name):
            return {"200": {"content": {"*/*": {"schema": {"properties": {"data": {"$ref": f"#/components/schemas/{ref_name}"}}}}}}}

        def _bool_resp():
            return {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultBoolean"}}}}}

        def _long_resp():
            return {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}}

        def _json_req(props, req=()):
            return {"content": {"application/json": {"schema": {"properties": props, **({"required": list(req)} if req else {})}}}}

        docs = {
            "paths": {
                "/admin-api/hrm/salary/option/list": {"get": {"responses": {"200": {"content": {"*/*": {"schema": {"properties": {"data": {"type": "array", "items": {"$ref": "#/components/schemas/SalaryOpt"}}}}}}}}}},
                "/admin-api/hrm/salary/option/update-enabled": {"put": {"tags": ["薪资项"], "requestBody": _json_req({"id": {"type": "integer"}, "enabled": {"type": "boolean"}}, ("id", "enabled")), "responses": _bool_resp()}},
                "/admin-api/bpm/model/list": {"get": {}},
                "/admin-api/bpm/model/get": {"get": {"responses": _ok_resp("BpmModel")}},
                "/admin-api/bpm/model/update-state": {"put": {"tags": ["流程模型"], "requestBody": _json_req({"id": {"type": "string"}, "state": {"type": "integer"}}, ("id", "state")), "responses": _bool_resp()}},
                "/admin-api/trade/brokerage-user/get": {"get": {"responses": _ok_resp("BrokerageUser")}},
                "/admin-api/trade/brokerage-user/update-brokerage-enable": {"put": {"tags": ["分销用户"], "requestBody": _json_req({"id": {"type": "integer"}, "brokerageEnabled": {"type": "boolean"}}, ("id", "brokerageEnabled")), "responses": _bool_resp()}},
                "/admin-api/mes/md/item/get": {"get": {"responses": _ok_resp("MdItem")}},
                "/admin-api/mes/md/item/update": {"put": {"tags": ["物料"], "requestBody": _json_req({"id": {"type": "integer"}, "remark": {"type": "string"}, "minStock": {"type": "number"}, "maxStock": {"type": "number"}}), "responses": _bool_resp()}},
                "/admin-api/hrm/salary/change-template/get": {"get": {"responses": _ok_resp("ChangeTpl")}},
                "/admin-api/hrm/salary/change-template/update": {"put": {"tags": ["调薪模板"], "requestBody": _json_req({"id": {"type": "integer"}, "name": {"type": "string"}, "options": {"type": "array", "items": {"$ref": "#/components/schemas/ChangeOpt"}}}), "responses": _bool_resp()}},
                "/admin-api/product/spu/get-detail": {"get": {"responses": _ok_resp("SpuDetail")}},
                "/admin-api/product/spu/update": {"put": {"tags": ["商品"], "requestBody": _json_req({"id": {"type": "integer"}, "name": {"type": "string"}, "price": {"type": "integer"}, "marketPrice": {"type": "integer"}, "costPrice": {"type": "integer"}, "stock": {"type": "integer"}, "skus": {"type": "array", "items": {"$ref": "#/components/schemas/SkuItem"}}}), "responses": _bool_resp()}},
                "/admin-api/mes/dv/check-plan/get": {"get": {"responses": _ok_resp("DvPlan")}},
                "/admin-api/mes/dv/check-plan/create": {"post": {"tags": ["点检"], "requestBody": _json_req({"name": {"type": "string"}, "status": {"type": "integer"}}), "responses": _long_resp()}},
                "/admin-api/mes/wm/stocktaking-plan/get": {"get": {"responses": _ok_resp("WmPlan")}},
                "/admin-api/mes/wm/stocktaking-plan/create": {"post": {"tags": ["盘点"], "requestBody": _json_req({"name": {"type": "string"}, "status": {"type": "integer"}, "startTime": {"type": "string"}, "endTime": {"type": "string"}}), "responses": _long_resp()}},
                "/admin-api/ai/knowledge/get": {"get": {"responses": _ok_resp("AiKb")}},
                "/admin-api/ai/knowledge/create": {"post": {"tags": ["知识库"], "requestBody": _json_req({"name": {"type": "string"}, "embeddingModel": {"type": "string"}}), "responses": _long_resp()}},
                "/admin-api/wms/check-order/get": {"get": {"responses": _ok_resp("WmsCheck")}},
                "/admin-api/wms/check-order/update": {"put": {"tags": ["盘库"], "requestBody": _json_req({"id": {"type": "integer"}, "totalQuantity": {"type": "number"}, "totalPrice": {"type": "number"}, "details": {"type": "array", "items": {"$ref": "#/components/schemas/WmsDetail"}}}), "responses": _bool_resp()}},
                "/admin-api/oa/travel-reimbursement/get": {"get": {"responses": _ok_resp("OaTravelReimb")}},
                "/admin-api/oa/travel-reimbursement/update": {"put": {"tags": ["出差报销"], "requestBody": _json_req({"id": {"type": "integer"}, "days": {"type": "integer"}, "totalPrice": {"type": "number"}, "items": {"type": "array", "items": {"$ref": "#/components/schemas/ReimbItem"}}}), "responses": _bool_resp()}},
                "/admin-api/crm/contract/get": {"get": {"responses": _ok_resp("CrmContract")}},
                "/admin-api/crm/contract/update": {"put": {"tags": ["CRM合同"], "requestBody": _json_req({"id": {"type": "integer"}, "totalPrice": {"type": "number"}, "totalProductPrice": {"type": "number"}, "products": {"type": "array", "items": {"$ref": "#/components/schemas/CrmProd"}}}), "responses": _bool_resp()}},
                "/admin-api/ai/knowledge/document/page": {"get": {}},
                "/admin-api/ai/knowledge/segment/page": {"get": {"parameters": [{"name": "documentId", "in": "query", "required": True}]}},
                "/admin-api/ai/knowledge/segment/get": {"get": {"responses": _ok_resp("AiSeg")}},
                "/admin-api/ai/knowledge/segment/update-status": {"put": {"tags": ["知识分段"], "requestBody": _json_req({"id": {"type": "integer"}, "status": {"type": "integer"}}, ("id", "status")), "responses": _bool_resp()}},
                "/admin-api/system/menu/list-all-simple": {"get": {}},
            },
            "components": {"schemas": {
                "CommonResultBoolean": {"type": "object", "properties": {"code": {"type": "integer"}, "data": {"type": "boolean"}}},
                "CommonResultLong": {"type": "object", "properties": {"code": {"type": "integer"}, "data": {"type": "integer"}}},
                "SalaryOpt": {"type": "object", "properties": {"id": {"type": "integer"}, "name": {"type": "string"}, "enabled": {"type": "boolean"}}},
                "BpmModel": {"type": "object", "properties": {"id": {"type": "string"}, "name": {"type": "string"}, "deploymentState": {"type": "integer"}}},
                "BrokerageUser": {"type": "object", "properties": {"id": {"type": "integer"}, "nickname": {"type": "string"}, "brokerageEnabled": {"type": "boolean"}, "brokerageTime": {"type": "string"}}},
                "MdItem": {"type": "object", "properties": {"id": {"type": "integer"}, "remark": {"type": "string"}, "minStock": {"type": "number"}, "maxStock": {"type": "number"}}},
                "ChangeOpt": {"type": "object", "properties": {"code": {"type": "integer"}, "name": {"type": "string"}}},
                "ChangeTpl": {"type": "object", "properties": {"id": {"type": "integer"}, "name": {"type": "string"}, "options": {"type": "array", "items": {"$ref": "#/components/schemas/ChangeOpt"}}}},
                "SkuItem": {"type": "object", "properties": {"name": {"type": "string"}, "price": {"type": "integer"}}},
                "SpuDetail": {"type": "object", "properties": {"id": {"type": "integer"}, "name": {"type": "string"}, "price": {"type": "integer"}, "marketPrice": {"type": "integer"}, "costPrice": {"type": "integer"}, "stock": {"type": "integer"}, "skus": {"type": "array", "items": {"$ref": "#/components/schemas/SkuItem"}}}},
                "DvPlan": {"type": "object", "properties": {"id": {"type": "integer"}, "name": {"type": "string"}, "status": {"type": "integer"}}},
                "WmPlan": {"type": "object", "properties": {"id": {"type": "integer"}, "name": {"type": "string"}, "status": {"type": "integer"}, "startTime": {"type": "string"}, "endTime": {"type": "string"}}},
                "AiKb": {"type": "object", "properties": {"id": {"type": "integer"}, "name": {"type": "string"}, "embeddingModel": {"type": "string"}}},
                "WmsDetail": {"type": "object", "properties": {"quantity": {"type": "number"}}},
                "WmsCheck": {"type": "object", "properties": {"id": {"type": "integer"}, "totalQuantity": {"type": "number"}, "totalPrice": {"type": "number"}, "details": {"type": "array", "items": {"$ref": "#/components/schemas/WmsDetail"}}}},
                "ReimbItem": {"type": "object", "properties": {"price": {"type": "number"}}},
                "OaTravelReimb": {"type": "object", "properties": {"id": {"type": "integer"}, "days": {"type": "integer"}, "totalPrice": {"type": "number"}, "items": {"type": "array", "items": {"$ref": "#/components/schemas/ReimbItem"}}}},
                "CrmProd": {"type": "object", "properties": {"productId": {"type": "integer"}}},
                "CrmContract": {"type": "object", "properties": {"id": {"type": "integer"}, "totalPrice": {"type": "number"}, "totalProductPrice": {"type": "number"}, "products": {"type": "array", "items": {"$ref": "#/components/schemas/CrmProd"}}}},
                "AiSeg": {"type": "object", "properties": {"id": {"type": "integer"}, "content": {"type": "string"}, "status": {"type": "integer"}}},
            }},
        }
        port = YudaoConventionWritePort(RegClient(), NS(_documents=docs, _source=None, _ui=None))

        # 1. Bare read_record(spec=None) resolves list-backed status spec
        opt = port.read_record("hrm/salary/option", 99)
        self.assertEqual(opt["name"], "岗位补贴")

        # 2. UUID candidate suffix resolves bpm/model
        bpm_res = port.resolve_target("bpm/model", "离职审批（ID=3342b203-ce3a-11f1-b09f-2a95d9691e80）", spec=port.status_specs()["bpm/model"])
        self.assertEqual(bpm_res["id"], "3342b203-ce3a-11f1-b09f-2a95d9691e80")

        # 3. Workflow updatable refusals for erp/*, mes/dv/check-plan, mes/wm/stocktaking-plan
        with self.assertRaises(BusinessToolRequestError):
            port.check_record_updatable("erp/sale-order", {"id": 1, "status": 20})
        port.check_record_updatable("erp/sale-order", {"id": 2, "status": 10})
        with self.assertRaises(BusinessToolRequestError):
            port.check_record_updatable("mes/dv/check-plan", {"id": 1, "status": 1})
        port.check_record_updatable("mes/dv/check-plan", {"id": 1, "status": 0})
        with self.assertRaises(BusinessToolRequestError):
            port.check_record_updatable("mes/wm/stocktaking-plan", {"id": 1, "status": 0})
        port.check_record_updatable("mes/wm/stocktaking-plan", {"id": 1, "status": 1})

        # 4. system/menu ref endpoint typo fix & volatile / computed sets
        self.assertEqual(port._ref_endpoint_for("system/menu")[0], "system/menu/list-all-simple")
        self.assertIn("brokerageTime", port.status_specs()["trade/brokerage-user"].volatile_fields)
        self.assertEqual(port.update_specs()["mes/md/item"].volatile_fields & {"minStock", "maxStock"}, {"minStock", "maxStock"})
        self.assertIn("name", port.aggregate_update_specs()["hrm/salary/change-template"].collections[0].server_computed_fields)
        spu_agg = port.aggregate_update_specs()["product/spu"]
        self.assertEqual(spu_agg.server_computed_fields & {"price", "marketPrice", "costPrice", "stock"}, {"price", "marketPrice", "costPrice", "stock"})
        spu_rec = port.read_record("product/spu", 23, spec=spu_agg)
        self.assertEqual(spu_rec["skus"][0]["name"], "便携扩展坞")
        self.assertIn("status", port.create_specs()["mes/dv/check-plan"].server_computed_fields)
        self.assertTrue({"status", "startTime", "endTime"} <= port.create_specs()["mes/wm/stocktaking-plan"].server_computed_fields)
        self.assertIn("embeddingModel", port.create_specs()["ai/knowledge"].server_computed_fields)
        self.assertTrue({"totalQuantity", "totalPrice", "totalAmount"} <= port.aggregate_update_specs()["wms/check-order"].server_computed_fields)
        self.assertTrue({"days", "totalPrice", "invoiceCount"} <= port.aggregate_update_specs()["oa/travel-reimbursement"].server_computed_fields)
        self.assertTrue({"totalPrice", "totalProductPrice"} <= port.aggregate_update_specs()["crm/contract"].server_computed_fields)
        with self.assertRaises(BusinessToolRequestError):
            port.check_record_updatable("wms/receipt-order", {"id": 1, "status": 4})
        port.check_record_updatable("wms/receipt-order", {"id": 2, "status": 0})
        port.check_record_updatable("wms/item", {"id": 3, "status": 4})
        seg_rec = port.resolve_target("ai/knowledge/segment", "分段A（ID=701）", spec=port.status_specs()["ai/knowledge/segment"])
        self.assertEqual(seg_rec["id"], 701)

        # 5. Type-use annotated List<@NotNull(...) Item>, DO$Item inner class, and /controller/app/ ignore
        with tempfile.TemporaryDirectory() as tmp:
            troot = Path(tmp)
            oa_vo = troot / "server/yudao-module-oa/src/main/java/cn/iocoder/yudao/module/oa/controller/admin/reimbursement/vo"
            oa_do = troot / "server/yudao-module-oa/src/main/java/cn/iocoder/yudao/module/oa/dal/dataobject/reimbursement"
            oa_svc = troot / "server/yudao-module-oa/src/main/java/cn/iocoder/yudao/module/oa/service/reimbursement"
            oa_ctrl = oa_vo.parent
            spu_admin = troot / "server/yudao-module-mall/src/main/java/cn/iocoder/yudao/module/product/controller/admin/spu/vo"
            spu_app = troot / "server/yudao-module-mall/src/main/java/cn/iocoder/yudao/module/product/controller/app/spu/vo"
            spu_svc = troot / "server/yudao-module-mall/src/main/java/cn/iocoder/yudao/module/product/service/spu"
            for d in (oa_vo, oa_do, oa_svc, oa_ctrl, spu_admin, spu_app, spu_svc, spu_admin.parent, spu_app.parent):
                d.mkdir(parents=True, exist_ok=True)
            (oa_do / "OaReimbursementDO.java").write_text(
                "package cn.iocoder.yudao.module.oa.dal.dataobject.reimbursement;\n"
                "import java.math.BigDecimal; import java.util.List;\n"
                "public class OaReimbursementDO {\n"
                "  private Long id;\n"
                "  private List<Item> items;\n"
                "  public static class Item {\n"
                "    private Integer sort;\n"
                "    private BigDecimal price;\n"
                "  }\n"
                "}\n", encoding="utf-8",
            )
            (oa_vo / "OaReimbursementSaveReqVO.java").write_text(
                "package cn.iocoder.yudao.module.oa.controller.admin.reimbursement.vo;\n"
                "import java.math.BigDecimal; import java.util.List;\n"
                "public class OaReimbursementSaveReqVO {\n"
                "  private Long id;\n"
                "  private List<@NotNull(message = \"报销明细不能为空\") Item> items;\n"
                "  public static class Item {\n"
                "    private Integer sort;\n"
                "    private BigDecimal price;\n"
                "  }\n"
                "}\n", encoding="utf-8",
            )
            (oa_vo / "OaReimbursementRespVO.java").write_text(
                "package cn.iocoder.yudao.module.oa.controller.admin.reimbursement.vo;\n"
                "import cn.iocoder.yudao.module.oa.dal.dataobject.reimbursement.OaReimbursementDO;\n"
                "import java.util.List;\n"
                "public class OaReimbursementRespVO {\n"
                "  private Long id;\n"
                "  private List<OaReimbursementDO.Item> items;\n"
                "}\n", encoding="utf-8",
            )
            (oa_ctrl / "OaReimbursementController.java").write_text(
                "package cn.iocoder.yudao.module.oa.controller.admin.reimbursement;\n"
                "import cn.iocoder.yudao.module.oa.controller.admin.reimbursement.vo.*;\n"
                "@RequestMapping(\"/oa/reimbursement\")\n"
                "public class OaReimbursementController {\n"
                "  private OaReimbursementService reimbursementService;\n"
                "  @GetMapping(\"/get\") public CommonResult<OaReimbursementRespVO> get(@RequestParam(\"id\") Long id) { return reimbursementService.get(id); }\n"
                "  @PutMapping(\"/update\") public CommonResult<Boolean> update(@RequestBody OaReimbursementSaveReqVO reqVO) { return reimbursementService.update(reqVO); }\n"
                "}\n", encoding="utf-8",
            )
            (oa_svc / "OaReimbursementServiceImpl.java").write_text(
                "package cn.iocoder.yudao.module.oa.service.reimbursement;\n"
                "public class OaReimbursementServiceImpl {\n"
                "  public void update(OaReimbursementSaveReqVO reqVO) { mapper.updateById(reqVO); }\n"
                "}\n", encoding="utf-8",
            )
            spu_do = troot / "server/yudao-module-mall/src/main/java/cn/iocoder/yudao/module/product/dal/dataobject/spu"
            spu_do.mkdir(parents=True, exist_ok=True)
            (spu_do / "ProductSpuDO.java").write_text(
                "package cn.iocoder.yudao.module.product.dal.dataobject.spu;\n"
                "public class ProductSpuDO {\n  private Long id;\n  private String name;\n}\n", encoding="utf-8",
            )
            (spu_admin / "ProductSkuSaveReqVO.java").write_text(
                "package cn.iocoder.yudao.module.product.controller.admin.spu.vo;\n"
                "public class ProductSkuSaveReqVO {\n  private String name;\n  private Integer price;\n}\n", encoding="utf-8",
            )
            (spu_admin / "ProductSpuSaveReqVO.java").write_text(
                "package cn.iocoder.yudao.module.product.controller.admin.spu.vo;\n"
                "import java.util.List;\n"
                "public class ProductSpuSaveReqVO {\n  private Long id;\n  private String name;\n  private List<ProductSkuSaveReqVO> skus;\n}\n", encoding="utf-8",
            )
            (spu_admin / "ProductSkuRespVO.java").write_text(
                "package cn.iocoder.yudao.module.product.controller.admin.spu.vo;\n"
                "public class ProductSkuRespVO {\n  private Long id;\n  private String name;\n  private Integer price;\n}\n", encoding="utf-8",
            )
            (spu_admin / "ProductSpuRespVO.java").write_text(
                "package cn.iocoder.yudao.module.product.controller.admin.spu.vo;\n"
                "import java.util.List;\n"
                "public class ProductSpuRespVO {\n  private Long id;\n  private String name;\n  private List<ProductSkuRespVO> skus;\n}\n", encoding="utf-8",
            )
            (spu_app / "AppProductSpuDetailRespVO.java").write_text(
                "package cn.iocoder.yudao.module.product.controller.app.spu.vo;\n"
                "import java.util.List;\n"
                "public class AppProductSpuDetailRespVO {\n"
                "  private Long id;\n  private List<Sku> skus;\n"
                "  public static class Sku {\n    private Long id;\n    private Integer price;\n  }\n"
                "}\n", encoding="utf-8",
            )
            (spu_admin.parent / "ProductSpuController.java").write_text(
                "package cn.iocoder.yudao.module.product.controller.admin.spu;\n"
                "import cn.iocoder.yudao.module.product.controller.admin.spu.vo.*;\n"
                "@RequestMapping(\"/product/spu\")\n"
                "public class ProductSpuController {\n"
                "  private ProductSpuService spuService;\n"
                "  @GetMapping(\"/get-detail\") public CommonResult<ProductSpuRespVO> getDetail(@RequestParam(\"id\") Long id) { return spuService.get(id); }\n"
                "  @PutMapping(\"/update\") public CommonResult<Boolean> update(@RequestBody ProductSpuSaveReqVO reqVO) { return spuService.update(reqVO); }\n"
                "}\n", encoding="utf-8",
            )
            (spu_app.parent / "AppProductSpuController.java").write_text(
                "package cn.iocoder.yudao.module.product.controller.app.spu;\n"
                "import cn.iocoder.yudao.module.product.controller.app.spu.vo.*;\n"
                "@RequestMapping(\"/product/spu\")\n"
                "public class AppProductSpuController {\n"
                "  private ProductSpuService spuService;\n"
                "  @GetMapping(\"/get-detail\") public CommonResult<AppProductSpuDetailRespVO> getDetail(@RequestParam(\"id\") Long id) { return spuService.appGet(id); }\n"
                "}\n", encoding="utf-8",
            )
            (spu_svc / "ProductSpuServiceImpl.java").write_text(
                "package cn.iocoder.yudao.module.product.service.spu;\n"
                "public class ProductSpuServiceImpl {\n"
                "  public void update(ProductSpuSaveReqVO reqVO) { mapper.updateById(reqVO); }\n"
                "}\n", encoding="utf-8",
            )
            (spu_app.parent / "AppProductSpuController.java").write_text(
                "package cn.iocoder.yudao.module.product.controller.app.spu;\n"
                "import cn.iocoder.yudao.module.product.controller.app.spu.vo.*;\n"
                "@RequestMapping(\"/product/spu\")\n"
                "public class AppProductSpuController {\n"
                "  @GetMapping(\"/get-detail\") public CommonResult<AppProductSpuDetailRespVO> getDetail(@RequestParam(\"id\") Long id) { return svc.appGet(id); }\n"
                "}\n", encoding="utf-8",
            )
            ast_docs = {
                "paths": {
                    "/admin-api/oa/reimbursement/get": {"get": {"responses": _ok_resp("OaReimb")}},
                    "/admin-api/oa/reimbursement/update": {"put": {"tags": ["费用报销"], "requestBody": _json_req({"id": {"type": "integer"}, "items": {"type": "array", "items": {"$ref": "#/components/schemas/Item"}}}), "responses": _bool_resp()}},
                    "/admin-api/product/spu/get-detail": {"get": {"responses": _ok_resp("SpuDetail")}},
                    "/admin-api/product/spu/update": {"put": {"tags": ["商品"], "requestBody": _json_req({"id": {"type": "integer"}, "name": {"type": "string"}, "skus": {"type": "array", "items": {"$ref": "#/components/schemas/SkuItem"}}}), "responses": _bool_resp()}},
                },
                "components": {"schemas": {
                    "CommonResultBoolean": {"type": "object", "properties": {"code": {"type": "integer"}, "data": {"type": "boolean"}}},
                    "Item": {"type": "object", "properties": {"id": {"type": "integer"}, "sort": {"type": "integer"}}},
                    "OaReimb": {"type": "object", "properties": {"id": {"type": "integer"}, "items": {"type": "array", "items": {"$ref": "#/components/schemas/Item"}}}},
                    "SkuItem": {"type": "object", "properties": {"name": {"type": "string"}, "price": {"type": "integer"}}},
                    "SpuDetail": {"type": "object", "properties": {"id": {"type": "integer"}, "name": {"type": "string"}, "skus": {"type": "array", "items": {"$ref": "#/components/schemas/SkuItem"}}}},
                }},
            }
            ast_port = YudaoConventionWritePort(RegClient(), NS(_documents=ast_docs, _source=YudaoSource(troot, release="v-step14-ast"), _ui=None))
            reimb_agg = ast_port.aggregate_update_specs()["oa/reimbursement"]
            self.assertIn("price", [f.name for f in reimb_agg.collections[0].fields])
            spu_ast_agg = ast_port.aggregate_update_specs()["product/spu"]
            self.assertIn("name", [f.name for f in spu_ast_agg.collections[0].fields])
            self.assertIn("name", spu_ast_agg.collections[0].server_computed_fields)

    def test_warm_write_catalog_populates_cache_and_releases_source_lock(self):
        import threading
        from unittest.mock import patch
        from web_operation_agent_yudao.writes import convention_writes as cw
        from web_operation_agent_yudao.writes.convention_writes import warm_yudao_write_catalog
        from web_operation_agent_yudao.rules import compiled_write_source as cws

        with tempfile.TemporaryDirectory() as tmp:
            troot = Path(tmp)
            _write_synthetic_release(troot)
            mod = troot / "server" / "yudao-module-demo" / "src" / "main" / "java" / "cn" / "iocoder" / "yudao" / "module" / "demo"
            for sub in ("controller", "controller/vo", "dal/dataobject", "service"):
                (mod / sub).mkdir(parents=True, exist_ok=True)
            (mod / "dal" / "dataobject" / "ItemDO.java").write_text(
                "package cn.iocoder.yudao.module.demo.dal.dataobject;\n"
                "public class ItemDO { private Long id; private String name; }\n",
                encoding="utf-8",
            )
            (mod / "controller" / "vo" / "ItemSaveReqVO.java").write_text(
                "package cn.iocoder.yudao.module.demo.controller.vo;\n"
                "public class ItemSaveReqVO { private Long id; private String name; }\n",
                encoding="utf-8",
            )
            (mod / "controller" / "ItemController.java").write_text(
                "package cn.iocoder.yudao.module.demo.controller;\n"
                "import cn.iocoder.yudao.module.demo.controller.vo.*;\n"
                '@RequestMapping("/demo/item")\n'
                "public class ItemController {\n"
                "  private ItemService itemService;\n"
                '  @PostMapping("/create") public CommonResult<Long> create(@RequestBody ItemSaveReqVO req) { return success(itemService.createItem(req)); }\n'
                '  @PutMapping("/update") public CommonResult<Boolean> update(@RequestBody ItemSaveReqVO req) { itemService.updateItem(req); return success(true); }\n'
                "}\n",
                encoding="utf-8",
            )
            (mod / "service" / "ItemServiceImpl.java").write_text(
                "package cn.iocoder.yudao.module.demo.service;\n"
                "public class ItemServiceImpl implements ItemService {\n"
                "  private ItemMapper itemMapper;\n"
                "  public Long createItem(ItemSaveReqVO req) { ItemDO d = BeanUtils.toBean(req, ItemDO.class); itemMapper.insert(d); return d.getId(); }\n"
                "  public void updateItem(ItemSaveReqVO req) { ItemDO d = BeanUtils.toBean(req, ItemDO.class); itemMapper.updateById(d); }\n"
                "}\n",
                encoding="utf-8",
            )
            docs = {
                "paths": {
                    "/admin-api/demo/item/get": {"get": {"responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/ItemResp"}}}}}}},
                    "/admin-api/demo/item/create": {"post": {"tags": ["管理后台 - 示例"], "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/ItemReq"}}}}, "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}}}},
                    "/admin-api/demo/item/update": {"put": {"tags": ["管理后台 - 示例"], "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/ItemReq"}}}}, "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultBoolean"}}}}}}},
                },
                "components": {"schemas": {
                    "CommonResultLong": {"type": "object", "properties": {"code": {"type": "integer"}, "data": {"type": "integer"}}},
                    "CommonResultBoolean": {"type": "object", "properties": {"code": {"type": "integer"}, "data": {"type": "boolean"}}},
                    "ItemReq": {"type": "object", "required": ["name"], "properties": {"id": {"type": "integer"}, "name": {"type": "string", "description": "名称"}}},
                    "ItemResp": {"type": "object", "properties": {"data": {"type": "object", "properties": {"id": {"type": "integer"}, "name": {"type": "string"}}}}},
                }},
            }
            source = YudaoSource(troot, release="v-warm-test")
            source.warm()

            orig_java_files = source._java_files
            service_calls = []

            def counting_java_files(suffix):
                if suffix == "ServiceImpl.java":
                    service_calls.append(suffix)
                return orig_java_files(suffix)

            peer_acquired = []

            def probe_ui_meta(_src, _ui=None):
                ok = False
                def worker():
                    nonlocal ok
                    if source._instance_lock.acquire(blocking=False):
                        ok = True
                        source._instance_lock.release()
                t = threading.Thread(target=worker)
                t.start()
                t.join(timeout=5)
                peer_acquired.append(ok)
                return cw._compile_ui_metadata(_src, _ui)

            with patch.object(source, "_java_files", side_effect=counting_java_files):
                cws.compile_yudao_write_catalog(
                    docs=docs,
                    source=source,
                    ui=None,
                    dedicated_creates=frozenset(),
                    dedicated_updates=frozenset(),
                    dedicated_statuses=frozenset(),
                    ui_metadata_fn=probe_ui_meta,
                )
            self.assertEqual(len(service_calls), 1)
            self.assertEqual(peer_acquired, [True])

            docs2 = copy.deepcopy(docs)
            warm_yudao_write_catalog(docs=docs2, source=source, ui=None)
            port = YudaoConventionWritePort(FakeYudaoClient(), FakeGoalData(docs2, source=source))
            with patch.object(cw, "compile_yudao_write_catalog", side_effect=AssertionError("expected warm cache hit")):
                updates = port.update_specs()
            self.assertIn("demo/item", updates)

    def test_b3_f1_nested_vo_schema_label_boundary_and_approval_card_labels(self):
        from web_operation_agent.business.compiled_writes import CompiledAggregateUpdateHandler

        def _json_req(props, required=None):
            return {"content": {"application/json": {"schema": {"type": "object", "required": list(required or ()), "properties": props}}}}

        def _bool_resp():
            return {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultBoolean"}}}}}

        with tempfile.TemporaryDirectory() as tmp:
            troot = Path(tmp)
            _write_synthetic_release(troot)
            erp_mod = troot / "server/yudao-module-erp/src/main/java/cn/iocoder/yudao/module/erp"
            (erp_mod / "controller/admin/sale/vo").mkdir(parents=True, exist_ok=True)
            (erp_mod / "dal/dataobject/sale").mkdir(parents=True, exist_ok=True)
            (erp_mod / "service/sale").mkdir(parents=True, exist_ok=True)
            (erp_mod / "dal/dataobject/sale/ErpSaleOrderDO.java").write_text(
                "package cn.iocoder.yudao.module.erp.dal.dataobject.sale;\n"
                "public class ErpSaleOrderDO { private Long id; private String remark; }\n",
                encoding="utf-8",
            )
            (erp_mod / "controller/admin/sale/vo/ErpSaleOrderSaveReqVO.java").write_text(
                "package cn.iocoder.yudao.module.erp.controller.admin.sale.vo;\n"
                "import io.swagger.v3.oas.annotations.media.Schema;\n"
                "import jakarta.validation.constraints.NotNull;\n"
                "import java.math.BigDecimal;\n"
                "import java.util.List;\n"
                "public class ErpSaleOrderSaveReqVO {\n"
                "    private Long id;\n"
                "    @Schema(description = \"备注\")\n"
                "    private String remark;\n"
                "    @Schema(description = \"订单清单列表\")\n"
                "    private List<Item> items;\n"
                "    public static class Item {\n"
                "        @Schema(description = \"订单项编号\")\n"
                "        private Long id;\n"
                "        @Schema(description = \"产品单价\")\n"
                "        private BigDecimal productPrice;\n"
                "        @Schema(description = \"产品数量\")\n"
                "        @NotNull\n"
                "        private BigDecimal count;\n"
                "        private String rawCode;\n"
                "        // @Schema(description = \"旧注释标签\")\n"
                "        private String commentFollowing;\n"
                "    }\n"
                "}\n",
                encoding="utf-8",
            )
            (erp_mod / "controller/admin/sale/vo/ErpSaleOrderRespVO.java").write_text(
                "package cn.iocoder.yudao.module.erp.controller.admin.sale.vo;\n"
                "import io.swagger.v3.oas.annotations.media.Schema;\n"
                "import java.math.BigDecimal;\n"
                "import java.util.List;\n"
                "public class ErpSaleOrderRespVO {\n"
                "    private Long id;\n"
                "    private String remark;\n"
                "    private List<Item> items;\n"
                "    public static class Item {\n"
                "        private Long id;\n"
                "        @Schema(description = \"产品单价\")\n"
                "        private BigDecimal productPrice;\n"
                "        @Schema(description = \"产品数量\")\n"
                "        private BigDecimal count;\n"
                "        private String rawCode;\n"
                "        private String commentFollowing;\n"
                "    }\n"
                "}\n",
                encoding="utf-8",
            )
            (erp_mod / "controller/admin/sale/ErpSaleOrderController.java").write_text(
                "package cn.iocoder.yudao.module.erp.controller.admin.sale;\n"
                "import cn.iocoder.yudao.module.erp.controller.admin.sale.vo.*;\n"
                "@RequestMapping(\"/erp/sale-order\")\n"
                "public class ErpSaleOrderController {\n"
                "    private ErpSaleOrderService saleOrderService;\n"
                "    @GetMapping(\"/get\") public CommonResult<ErpSaleOrderRespVO> get(@RequestParam(\"id\") Long id) { return success(saleOrderService.get(id)); }\n"
                "    @PutMapping(\"/update\") public CommonResult<Boolean> update(@RequestBody ErpSaleOrderSaveReqVO req) { saleOrderService.update(req); return success(true); }\n"
                "}\n",
                encoding="utf-8",
            )
            (erp_mod / "service/sale/ErpSaleOrderServiceImpl.java").write_text(
                "package cn.iocoder.yudao.module.erp.service.sale;\n"
                "public class ErpSaleOrderServiceImpl {\n"
                "    public void update(ErpSaleOrderSaveReqVO req) { mapper.updateById(req); }\n"
                "}\n",
                encoding="utf-8",
            )
            # Simulate OpenAPI shared "Item" collision where Item lacks productPrice & has conflicting count description
            docs = {
                "paths": {
                    "/admin-api/erp/sale-order/page": {"get": {"responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/ListPage"}}}}}}},
                    "/admin-api/erp/sale-order/get": {"get": {"responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/SaleResp"}}}}}}},
                    "/admin-api/erp/sale-order/update": {
                        "put": {
                            "tags": ["管理后台 - ERP 销售订单"],
                            "requestBody": _json_req({
                                "id": {"type": "integer"},
                                "remark": {"type": "string"},
                                "items": {"type": "array", "items": {"$ref": "#/components/schemas/Item"}},
                            }),
                            "responses": _bool_resp(),
                        }
                    },
                },
                "components": {
                    "schemas": {
                        "CommonResultBoolean": {"type": "object", "properties": {"code": {"type": "integer"}, "data": {"type": "boolean"}}},
                        "ListPage": {"type": "object", "properties": {"data": {"type": "object", "properties": {"list": {"type": "array", "items": {"type": "object"}}}}}},
                        "Item": {"type": "object", "properties": {"id": {"type": "integer"}, "count": {"type": "number", "description": "碰撞后的错误数量描述"}}},
                        "SaleResp": {"type": "object", "properties": {"data": {"type": "object", "properties": {"id": {"type": "integer"}, "remark": {"type": "string"}, "items": {"type": "array", "items": {"$ref": "#/components/schemas/Item"}}}}}},
                    }
                },
            }

            class SaleClient(FakeYudaoClient):
                def __init__(self):
                    super().__init__()
                    self.records[("erp/sale-order", 10)] = {
                        "id": 10,
                        "name": "销售订单10",
                        "remark": "原备注",
                        "items": [{"id": 101, "productPrice": 12.5, "count": 2, "rawCode": "R1", "commentFollowing": "C1"}],
                    }

            port = YudaoConventionWritePort(SaleClient(), FakeGoalData(docs, source=YudaoSource(troot, release="v-b3-f1")))
            agg = port.aggregate_update_specs()["erp/sale-order"]
            by_name = {f.name: f for f in agg.collections[0].fields}
            self.assertEqual(by_name["productPrice"].label, "产品单价")
            self.assertEqual(by_name["count"].label, "产品数量")
            self.assertEqual(by_name["rawCode"].label, "rawCode")
            self.assertEqual(by_name["commentFollowing"].label, "commentFollowing")

            handler = CompiledAggregateUpdateHandler(port)
            spec = handler.prepare(
                {
                    "entity": "erp/sale-order",
                    "target": "销售订单10（ID=10）",
                    "line_changes": [{"collection": "items", "match": "#1", "changes": [{"field": "count", "value": 5}]}],
                },
                context=_ctx(),
            )
            detail_labels = [d.label for d in spec.details]
            self.assertTrue(any("产品数量" in lbl for lbl in detail_labels))
            self.assertFalse(any("产品单价" in lbl for lbl in detail_labels))

    def test_b3_fix2_mq_producer_client_template_and_auth_config_hard_exclusions(self):
        from web_operation_agent_yudao.rules import compiled_write_source as cws

        with tempfile.TemporaryDirectory() as tmp:
            troot = Path(tmp)
            _write_synthetic_release(troot)
            sys_mod = troot / "server/yudao-module-system/src/main/java/cn/iocoder/yudao/module/system"
            pms_mod = troot / "server/yudao-module-pms/src/main/java/cn/iocoder/yudao/module/pms"
            crm_mod = troot / "server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm"
            for p in (
                sys_mod / "controller/admin/user/vo", sys_mod / "service/user", sys_mod / "dal/dataobject/user",
                sys_mod / "controller/admin/tenant/vo", sys_mod / "service/tenant", sys_mod / "dal/dataobject/tenant",
                sys_mod / "controller/admin/permission/vo", sys_mod / "service/permission", sys_mod / "dal/dataobject/permission",
                sys_mod / "controller/admin/oauth2/vo", sys_mod / "service/oauth2", sys_mod / "dal/dataobject/oauth2",
                sys_mod / "controller/admin/social/vo", sys_mod / "service/social", sys_mod / "dal/dataobject/social",
                pms_mod / "controller/admin/kb/vo", pms_mod / "service/kb", pms_mod / "dal/dataobject/kb",
                crm_mod / "controller/admin/contract/vo", crm_mod / "service/contract", crm_mod / "dal/dataobject/contract",
            ):
                p.mkdir(parents=True, exist_ok=True)

            # 1) User update calling publishUserProfileUpdatedIfChanged -> adminUserProducer (MQ) must be blocked
            (sys_mod / "dal/dataobject/user/AdminUserDO.java").write_text("package p;\npublic class AdminUserDO { private Long id; private String nickname; }\n", encoding="utf-8")
            (sys_mod / "controller/admin/user/vo/UserSaveReqVO.java").write_text("package p;\npublic class UserSaveReqVO { private Long id; private String nickname; }\n", encoding="utf-8")
            (sys_mod / "controller/admin/user/vo/UserProfileUpdateReqVO.java").write_text("package p;\npublic class UserProfileUpdateReqVO { private String nickname; }\n", encoding="utf-8")
            (sys_mod / "controller/admin/user/UserController.java").write_text(
                "package p;\n@RequestMapping(\"/system/user\")\npublic class UserController {\n"
                "  private AdminUserService userService;\n"
                "  @PutMapping(\"/update\") public CommonResult<Boolean> update(@RequestBody UserSaveReqVO req) { userService.updateUser(req); return success(true); }\n"
                "}\n", encoding="utf-8",
            )
            (sys_mod / "controller/admin/user/UserProfileController.java").write_text(
                "package p;\n@RequestMapping(\"/system/user/profile\")\npublic class UserProfileController {\n"
                "  private AdminUserService userService;\n"
                "  @PutMapping(\"/update\") public CommonResult<Boolean> update(@RequestBody UserProfileUpdateReqVO req) { userService.updateUserProfile(1L, req); return success(true); }\n"
                "}\n", encoding="utf-8",
            )
            (sys_mod / "service/user/AdminUserServiceImpl.java").write_text(
                "package p;\npublic class AdminUserServiceImpl {\n"
                "  private AdminUserMapper userMapper;\n"
                "  private AdminUserProducer adminUserProducer;\n"
                "  public void updateUser(UserSaveReqVO req) { userMapper.updateById(req); publishUserProfileUpdatedIfChanged(req); }\n"
                "  public void updateUserProfile(Long id, UserProfileUpdateReqVO req) { userMapper.updateById(req); publishUserProfileUpdatedIfChanged(req); }\n"
                "  private void publishUserProfileUpdatedIfChanged(Object req) { adminUserProducer.sendUserProfileUpdateMessage(1L, \"a\", null); }\n"
                "}\n", encoding="utf-8",
            )
            # 2) Client & Template field dependency calls must be rejected
            (crm_mod / "dal/dataobject/contract/DemoClientDO.java").write_text("package p;\npublic class DemoClientDO { private Long id; private String name; }\n", encoding="utf-8")
            (crm_mod / "controller/admin/contract/vo/DemoClientVO.java").write_text("package p;\npublic class DemoClientVO { private Long id; private String name; }\n", encoding="utf-8")
            (crm_mod / "controller/admin/contract/DemoClientController.java").write_text(
                "package p;\n@RequestMapping(\"/demo/client-call\")\npublic class DemoClientController {\n"
                "  private DemoClientCallService clientCallService;\n"
                "  @PutMapping(\"/update\") public CommonResult<Boolean> u(@RequestBody DemoClientVO r) { clientCallService.update(r); return success(true); }\n"
                "}\n", encoding="utf-8",
            )
            (crm_mod / "service/contract/DemoClientCallServiceImpl.java").write_text(
                "package p;\npublic class DemoClientCallServiceImpl {\n"
                "  private SmsClient smsClient;\n"
                "  public void update(DemoClientVO r) { mapper.updateById(r); smsClient.sendSms(1L); }\n"
                "}\n", encoding="utf-8",
            )
            (crm_mod / "controller/admin/contract/DemoTemplateController.java").write_text(
                "package p;\n@RequestMapping(\"/demo/template-call\")\npublic class DemoTemplateController {\n"
                "  private DemoTemplateCallService templateCallService;\n"
                "  @PutMapping(\"/update\") public CommonResult<Boolean> u(@RequestBody DemoClientVO r) { templateCallService.update(r); return success(true); }\n"
                "}\n", encoding="utf-8",
            )
            (crm_mod / "service/contract/DemoTemplateCallServiceImpl.java").write_text(
                "package p;\npublic class DemoTemplateCallServiceImpl {\n"
                "  private StringRedisTemplate cache;\n"
                "  public void update(DemoClientVO r) { mapper.updateById(r); cache.delete(\"k\"); }\n"
                "}\n", encoding="utf-8",
            )
            # 3) Permission / Role / Menu / Tenant / TenantPackage / OAuth2Client / SocialClient / ContentPermission
            for mod_dir, ctrl_name, svc_name, path_prefix, vo_name in (
                (sys_mod / "controller/admin/permission", "RoleController", "RoleService", "/system/role", "RoleVO"),
                (sys_mod / "controller/admin/permission", "MenuController", "MenuService", "/system/menu", "MenuVO"),
                (sys_mod / "controller/admin/tenant", "TenantController", "TenantService", "/system/tenant", "TenantVO"),
                (sys_mod / "controller/admin/tenant", "TenantPackageController", "TenantPackageService", "/system/tenant-package", "TenantPkgVO"),
                (sys_mod / "controller/admin/oauth2", "OAuth2ClientController", "OAuth2ClientService", "/system/oauth2-client", "OAuth2ClientVO"),
                (sys_mod / "controller/admin/social", "SocialClientController", "SocialClientService", "/system/social-client", "SocialClientVO"),
                (pms_mod / "controller/admin/kb", "PmsKnowledgeContentPermissionController", "PmsKnowledgeContentPermissionService", "/pms/kb/content-permission", "PermVO"),
            ):
                (mod_dir / "vo" / f"{vo_name}.java").write_text(f"package p;\npublic class {vo_name} {{ private Long id; private String name; }}\n", encoding="utf-8")
                (mod_dir / f"{ctrl_name}.java").write_text(
                    f"package p;\n@RequestMapping(\"{path_prefix}\")\npublic class {ctrl_name} {{\n"
                    f"  private {svc_name} targetService;\n"
                    f"  @PostMapping(\"/create\") public CommonResult<Long> c(@RequestBody {vo_name} r) {{ return success(targetService.create(r)); }}\n"
                    f"  @PutMapping(\"/update\") public CommonResult<Boolean> u(@RequestBody {vo_name} r) {{ targetService.update(r); return success(true); }}\n"
                    "}\n", encoding="utf-8",
                )
            for svc_dir, svc_impl in (
                (sys_mod / "service/permission", "RoleServiceImpl"),
                (sys_mod / "service/permission", "MenuServiceImpl"),
                (sys_mod / "service/tenant", "TenantServiceImpl"),
                (sys_mod / "service/tenant", "TenantPackageServiceImpl"),
                (sys_mod / "service/oauth2", "OAuth2ClientServiceImpl"),
                (sys_mod / "service/social", "SocialClientServiceImpl"),
                (pms_mod / "service/kb", "PmsKnowledgeContentPermissionServiceImpl"),
            ):
                (svc_dir / f"{svc_impl}.java").write_text(
                    f"package p;\npublic class {svc_impl} {{\n"
                    "  public Long create(Object r) { mapper.insert(r); return 1L; }\n"
                    "  public void update(Object r) { mapper.updateById(r); }\n"
                    "}\n", encoding="utf-8",
                )
            # 4) CrmPermissionService.createPermission remains allowed (B3 companion allowlist preserved)
            (crm_mod / "controller/admin/contract/CrmContractController.java").write_text(
                "package p;\n@RequestMapping(\"/crm/contract-demo\")\npublic class CrmContractController {\n"
                "  private CrmContractDemoService contractDemoService;\n"
                "  @PostMapping(\"/create\") public CommonResult<Long> c(@RequestBody DemoClientVO r) { return success(contractDemoService.create(r)); }\n"
                "}\n", encoding="utf-8",
            )
            (crm_mod / "service/contract/CrmContractDemoServiceImpl.java").write_text(
                "package p;\npublic class CrmContractDemoServiceImpl {\n"
                "  private CrmPermissionService crmPermissionService;\n"
                "  public Long create(DemoClientVO r) { mapper.insert(r); crmPermissionService.createPermission(1L); return 1L; }\n"
                "}\n", encoding="utf-8",
            )

            schemas = {
                "CommonResultLong": {"type": "object", "properties": {"code": {"type": "integer"}, "data": {"type": "integer"}}},
                "CommonResultBoolean": {"type": "object", "properties": {"code": {"type": "integer"}, "data": {"type": "boolean"}}},
                "SimpleReq": {"type": "object", "properties": {"id": {"type": "integer"}, "name": {"type": "string"}, "nickname": {"type": "string"}}},
                "SimpleResp": {"type": "object", "properties": {"data": {"type": "object", "properties": {"id": {"type": "integer"}, "name": {"type": "string"}, "nickname": {"type": "string"}}}}},
            }
            paths = {}
            for ent in (
                "system/user", "system/user/profile", "demo/client-call", "demo/template-call",
                "system/role", "system/menu", "system/tenant", "system/tenant-package",
                "system/oauth2-client", "system/social-client", "pms/kb/content-permission", "crm/contract-demo",
            ):
                paths[f"/admin-api/{ent}/get"] = {"get": {"responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/SimpleResp"}}}}}}}
                if ent not in {"system/user", "system/user/profile", "demo/client-call", "demo/template-call"}:
                    paths[f"/admin-api/{ent}/create"] = {"post": {"requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/SimpleReq"}}}}, "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}}}}
                paths[f"/admin-api/{ent}/update"] = {"put": {"requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/SimpleReq"}}}}, "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultBoolean"}}}}}}}
            docs = {"paths": paths, "components": {"schemas": schemas}}
            src = YudaoSource(troot, release="v-b3-fix2")
            cat = cws.compile_yudao_write_catalog(
                docs=docs, source=src, ui=None, dedicated_creates=frozenset(),
                dedicated_updates=frozenset(), dedicated_statuses=frozenset(),
                ui_metadata_fn=lambda _s, _u=None: ({}, {}, {}),
            )
            for blocked_ent in (
                "system/user", "system/user/profile", "demo/client-call", "demo/template-call",
                "system/role", "system/menu", "system/tenant", "system/tenant-package",
                "system/oauth2-client", "system/social-client", "pms/kb/content-permission",
            ):
                self.assertNotIn(blocked_ent, cat["updates"], f"{blocked_ent} leaked into updates")
                self.assertNotIn(blocked_ent, cat["creates"], f"{blocked_ent} leaked into creates")
            self.assertIn("crm/contract-demo", cat["creates"])

            # Direct _analyze_service_method check confirms service-type blocking even outside _blocked(entity)
            self.assertFalse(cws._analyze_service_method(src, cws.ControllerEndpointAST("p", "Put", "TenantPackageService", "update", "", "", ()))[0])
            self.assertFalse(cws._analyze_service_method(src, cws.ControllerEndpointAST("p", "Put", "PmsKnowledgeContentPermissionService", "update", "", "", ()))[0])
            self.assertFalse(cws._analyze_service_method(src, cws.ControllerEndpointAST("p", "Put", "OAuth2ClientService", "update", "", "", ()))[0])
            self.assertFalse(cws._analyze_service_method(src, cws.ControllerEndpointAST("p", "Put", "SocialClientService", "update", "", "", ()))[0])

    def test_b3_fix3_cond_side_effect_fields_kept_in_spec_and_immutable_fields(self):
        from web_operation_agent.business.convention_writes import ConventionUpdateHandler

        def _json_req(props, required=None):
            return {"content": {"application/json": {"schema": {"type": "object", "required": list(required or ()), "properties": props}}}}

        def _bool_resp():
            return {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultBoolean"}}}}}

        with tempfile.TemporaryDirectory() as tmp:
            troot = Path(tmp)
            _write_synthetic_release(troot)
            mem_mod = troot / "server/yudao-module-member/src/main/java/cn/iocoder/yudao/module/member"
            for p in (mem_mod / "controller/admin/user/vo", mem_mod / "dal/dataobject/user", mem_mod / "service/user"):
                p.mkdir(parents=True, exist_ok=True)
            (mem_mod / "dal/dataobject/user/MemberUserDO.java").write_text(
                "package p;\npublic class MemberUserDO { private Long id; private Integer status; private String mark; }\n",
                encoding="utf-8",
            )
            (mem_mod / "controller/admin/user/vo/MemberUserUpdateReqVO.java").write_text(
                "package p;\nimport jakarta.validation.constraints.NotNull;\n"
                "public class MemberUserUpdateReqVO {\n"
                "  @NotNull private Long id;\n"
                "  @NotNull private Integer status;\n"
                "  private String mark;\n"
                "}\n",
                encoding="utf-8",
            )
            (mem_mod / "controller/admin/user/vo/MemberUserRespVO.java").write_text(
                "package p;\npublic class MemberUserRespVO { private Long id; private Integer status; private String mark; }\n",
                encoding="utf-8",
            )
            (mem_mod / "controller/admin/user/MemberUserController.java").write_text(
                "package p;\n@RequestMapping(\"/member/user\")\npublic class MemberUserController {\n"
                "  private MemberUserService userService;\n"
                "  @GetMapping(\"/get\") public CommonResult<MemberUserRespVO> get(@RequestParam(\"id\") Long id) { return success(userService.getUser(id)); }\n"
                "  @PutMapping(\"/update\") public CommonResult<Boolean> update(@RequestBody MemberUserUpdateReqVO req) { userService.updateUser(req); return success(true); }\n"
                "}\n",
                encoding="utf-8",
            )
            (mem_mod / "service/user/MemberUserServiceImpl.java").write_text(
                "package p;\npublic class MemberUserServiceImpl {\n"
                "  private MemberUserMapper memberUserMapper;\n"
                "  private OAuth2TokenCommonApi oauth2TokenApi;\n"
                "  public void updateUser(MemberUserUpdateReqVO req) {\n"
                "    MemberUserDO updateObj = BeanUtils.toBean(req, MemberUserDO.class);\n"
                "    memberUserMapper.updateById(updateObj);\n"
                "    if (CommonStatusEnum.isDisable(updateObj.getStatus())) {\n"
                "      oauth2TokenApi.removeAccessToken(updateObj.getId(), 1);\n"
                "    }\n"
                "  }\n"
                "}\n",
                encoding="utf-8",
            )
            docs = {
                "paths": {
                    "/admin-api/member/user/page": {"get": {"responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/ListPage"}}}}}}},
                    "/admin-api/member/user/get": {"get": {"responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/MemResp"}}}}}}},
                    "/admin-api/member/user/update": {
                        "put": {
                            "tags": ["管理后台 - 会员用户"],
                            "requestBody": _json_req({
                                "id": {"type": "integer"},
                                "status": {"type": "integer", "description": "状态"},
                                "mark": {"type": "string", "description": "备注"},
                            }, required=["id", "status"]),
                            "responses": _bool_resp(),
                        }
                    },
                },
                "components": {
                    "schemas": {
                        "CommonResultBoolean": {"type": "object", "properties": {"code": {"type": "integer"}, "data": {"type": "boolean"}}},
                        "ListPage": {"type": "object", "properties": {"data": {"type": "object", "properties": {"list": {"type": "array", "items": {"type": "object"}}}}}},
                        "MemResp": {"type": "object", "properties": {"data": {"type": "object", "properties": {"id": {"type": "integer"}, "status": {"type": "integer"}, "mark": {"type": "string"}}}}},
                    }
                },
            }

            class MemberClient(FakeYudaoClient):
                def __init__(self):
                    super().__init__()
                    self.records[("member/user", 42)] = {"id": 42, "name": "会员甲", "status": 0, "mark": "旧备注"}

            port = YudaoConventionWritePort(MemberClient(), FakeGoalData(docs, source=YudaoSource(troot, release="v-b3-fix3")))
            spec = port.update_specs()["member/user"]
            self.assertIn("status", [f.name for f in spec.fields])
            self.assertIn("status", port._immutable_fields.get("member/user", ()))

            handler = ConventionUpdateHandler(port)
            prop = handler.prepare(
                {"entity": "member/user", "target": "会员甲（ID=42）", "changes": [{"field": "mark", "value": "新备注"}]},
                context=_ctx(),
            )
            self.assertEqual(prop.args["payload"]["status"], 0)
            self.assertEqual(prop.args["payload"]["mark"], "新备注")
            with self.assertRaises(BusinessToolRequestError):
                handler.prepare(
                    {"entity": "member/user", "target": "会员甲（ID=42）", "changes": [{"field": "status", "value": 1}]},
                    context=_ctx(),
                )

    def test_b3_fix4_dangerous_service_word_boundaries(self):
        from web_operation_agent_yudao.rules import compiled_write_source as cws

        for blocked in (
            "BpmTaskService",
            "TaskService",
            "PayOrderService",
            "PayAppService",
            "BpmProcessInstanceApi",
            "BpmProcessInstanceService",
            "PayWalletService",
            "SmsSendService",
            "MailSendService",
            "NotifySendService",
            "AdminUserProducer",
        ):
            self.assertIsNotNone(cws._DANGEROUS_SERVICE_TYPES.search(blocked), f"Expected {blocked} blocked")

        for allowed in (
            "OaTaskService",
            "MesProTaskService",
            "MesWmStockTakingTaskService",
            "ErpFinancePaymentService",
        ):
            self.assertIsNone(cws._DANGEROUS_SERVICE_TYPES.search(allowed), f"Expected {allowed} allowed")

    def test_b3_fix5_yudao_unavailable_propose_operation_routes_to_compiled_tool(self):
        from web_operation_agent.business.convention_writes import resolve_compiled_write_fallback
        from web_operation_agent.goal.skill_writes import SkillWrites
        from web_operation_agent_yudao.host.application import YudaoApplication

        def _json_req(props, required=None):
            return {"content": {"application/json": {"schema": {"type": "object", "required": list(required or ()), "properties": props}}}}

        def _bool_resp():
            return {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultBoolean"}}}}}

        with tempfile.TemporaryDirectory() as tmp:
            troot = Path(tmp)
            _write_synthetic_release(troot)
            docs = {
                "paths": {
                    "/admin-api/demo/item/get": {"get": {"responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/ItemResp"}}}}}}},
                    "/admin-api/demo/item/update": {
                        "put": {
                            "tags": ["管理后台 - 示例"],
                            "requestBody": _json_req({"id": {"type": "integer"}, "name": {"type": "string"}}, required=["id"]),
                            "responses": _bool_resp(),
                        }
                    },
                },
                "components": {
                    "schemas": {
                        "CommonResultBoolean": {"type": "object", "properties": {"code": {"type": "integer"}, "data": {"type": "boolean"}}},
                        "ItemResp": {"type": "object", "properties": {"data": {"type": "object", "properties": {"id": {"type": "integer"}, "name": {"type": "string"}}}}},
                    }
                },
            }
            app = YudaoApplication("https://app.test", None, docs, client=FakeYudaoClient(), native_writes=True)
            writes = SkillWrites(app, NS(principal=_ctx().principal, mutations_enabled=True))
            defs = writes.available_definitions()
            self.assertEqual(resolve_compiled_write_fallback("demo_item_update", {}, defs), ("demo/item", ("update_record",)))
            msg = app.goal_data.write_route_capability().unavailable_operation_message(
                "demo_item_update",
                entity="demo/item",
                compiled_tools=("update_record",),
            )
            self.assertIn('update_record(entity="demo/item"', msg)
            self.assertNotIn("完成预填并用 then_highlight", msg)

    def test_b3_fix4_exclusions_for_unmentioned_name_rewrite_and_required_task_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            troot = Path(tmp)
            _write_synthetic_release(troot)
            work_mod = troot / "server/yudao-module-work/src/main/java/cn/iocoder/yudao/module/work"
            (work_mod / "controller/admin/task/vo").mkdir(parents=True, exist_ok=True)
            (work_mod / "service/task").mkdir(parents=True, exist_ok=True)
            (work_mod / "dal/dataobject/task").mkdir(parents=True, exist_ok=True)
            (work_mod / "dal/dataobject/task/WorkTaskDO.java").write_text(
                "package p;\npublic class WorkTaskDO { private Long id; private String name; private String remark; }\n",
                encoding="utf-8",
            )
            (work_mod / "controller/admin/task/vo/WorkTaskSaveReqVO.java").write_text(
                "package p;\npublic class WorkTaskSaveReqVO { private Long id; private String remark; }\n",
                encoding="utf-8",
            )
            (work_mod / "controller/admin/task/WorkTaskController.java").write_text(
                "package p;\n@RequestMapping(\"/work/task\")\npublic class WorkTaskController {\n"
                "  private WorkTaskService taskService;\n"
                "  @PutMapping(\"/update\") public CommonResult<Boolean> update(@RequestBody WorkTaskSaveReqVO reqVO) { taskService.updateTask(reqVO); return success(true); }\n"
                "}\n",
                encoding="utf-8",
            )
            (work_mod / "service/task/WorkTaskServiceImpl.java").write_text(
                "package p;\npublic class WorkTaskServiceImpl implements WorkTaskService {\n"
                "  private WorkTaskMapper mapper;\n"
                "  public void updateTask(WorkTaskSaveReqVO updateReqVO) {\n"
                "    WorkTaskDO updateObj = BeanUtils.toBean(updateReqVO, WorkTaskDO.class).setName(buildName());\n"
                "    mapper.updateById(updateObj);\n"
                "  }\n"
                "}\n",
                encoding="utf-8",
            )
            docs = {
                "paths": {
                    "/admin-api/work/task/get": {"get": {"responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/WorkResp"}}}}}}},
                    "/admin-api/work/task/update": {
                        "put": {
                            "tags": ["管理后台 - 工单任务"],
                            "requestBody": {"content": {"application/json": {"schema": {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}, "remark": {"type": "string"}}}}}},
                            "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultBoolean"}}}}},
                        }
                    },
                    "/admin-api/work/task-line/page": {
                        "get": {
                            "parameters": [{"name": "taskId", "in": "query", "required": True, "schema": {"type": "integer"}}],
                            "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultBoolean"}}}}},
                        }
                    },
                    "/admin-api/work/task-line/get": {"get": {"responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/LineResp"}}}}}}},
                    "/admin-api/work/task-line/create": {
                        "post": {
                            "tags": ["管理后台 - 任务行"],
                            "requestBody": {"content": {"application/json": {"schema": {"type": "object", "required": ["taskId"], "properties": {"taskId": {"type": "integer"}, "remark": {"type": "string"}}}}}},
                            "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}},
                        }
                    },
                    "/admin-api/work/task-line/update": {
                        "put": {
                            "tags": ["管理后台 - 任务行"],
                            "requestBody": {"content": {"application/json": {"schema": {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}, "remark": {"type": "string"}}}}}},
                            "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultBoolean"}}}}},
                        }
                    },
                },
                "components": {
                    "schemas": {
                        "CommonResultBoolean": {"type": "object", "properties": {"code": {"type": "integer"}, "data": {"type": "boolean"}}},
                        "CommonResultLong": {"type": "object", "properties": {"code": {"type": "integer"}, "data": {"type": "integer"}}},
                        "WorkResp": {"type": "object", "properties": {"data": {"type": "object", "properties": {"id": {"type": "integer"}, "name": {"type": "string"}, "remark": {"type": "string"}}}}},
                        "LineResp": {"type": "object", "properties": {"data": {"type": "object", "properties": {"id": {"type": "integer"}, "taskId": {"type": "integer"}, "remark": {"type": "string"}}}}},
                    }
                },
            }
            port = YudaoConventionWritePort(FakeYudaoClient(), FakeGoalData(docs, source=YudaoSource(troot, release="v-b3-fix4-excl")))
            updates = port.update_specs()
            creates = port.create_specs()
            self.assertNotIn("work/task", updates)
            self.assertNotIn("work/task-line", updates)
            self.assertIn("work/task-line", creates)

    def test_w0_2hop_positive_proof_security_boundaries_and_ai_chat_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            troot = Path(tmp)
            _write_synthetic_release(troot)
            mod = troot / "server/yudao-module-work/src/main/java/cn/iocoder/yudao/module/work"
            for sub in ("controller/admin/item/vo", "service/item", "service/helper", "dal/dataobject/item", "dal/mysql/item"):
                (mod / sub).mkdir(parents=True, exist_ok=True)
            (mod / "dal/dataobject/item/WorkItemDO.java").write_text(
                "@TableName(\"work_item\")\npublic class WorkItemDO { private Long id; private String name; }\n",
                encoding="utf-8",
            )
            (mod / "dal/dataobject/item/WorkLogDO.java").write_text(
                "@TableName(\"work_log\")\npublic class WorkLogDO { private Long id; private Long itemId; }\n",
                encoding="utf-8",
            )
            (mod / "controller/admin/item/vo/WorkItemSaveReqVO.java").write_text(
                "public class WorkItemSaveReqVO { private Long id; private String name; }\n",
                encoding="utf-8",
            )
            (mod / "controller/admin/item/vo/WorkItemRespVO.java").write_text(
                "public class WorkItemRespVO { private Long id; private String name; }\n",
                encoding="utf-8",
            )
            # Safe 2-hop helper service
            (mod / "service/helper/WorkHelperServiceImpl.java").write_text(
                "public class WorkHelperServiceImpl implements WorkHelperService {\n"
                "  private WorkLogMapper logMapper;\n"
                "  public void writeLog(Long itemId) { logMapper.insert(new WorkLogDO()); }\n"
                "}\n",
                encoding="utf-8",
            )
            # Async callee -> must be rejected
            (mod / "service/helper/AsyncHelperServiceImpl.java").write_text(
                "public class AsyncHelperServiceImpl implements AsyncHelperService {\n"
                "  private WorkLogMapper logMapper;\n"
                "  @Async\n"
                "  public void writeAsync(Long itemId) { logMapper.insert(new WorkLogDO()); }\n"
                "}\n",
                encoding="utf-8",
            )
            # Allowed owner CRM permission service + blocked RBAC/Menu/Async/MQ/Pay/Client
            (mod / "service/helper/CrmPermissionServiceImpl.java").write_text(
                "public class CrmPermissionServiceImpl implements CrmPermissionService {\n"
                "  private CrmPermissionMapper crmPermissionMapper;\n"
                "  public void createPermission(Long bizId) { crmPermissionMapper.insert(new CrmPermissionDO()); }\n"
                "}\n",
                encoding="utf-8",
            )
            (mod / "service/helper/AiChatRoleServiceImpl.java").write_text(
                "public class AiChatRoleServiceImpl implements AiChatRoleService {\n"
                "  private AiChatRoleMapper chatRoleMapper;\n"
                "  public Long createChatRole(WorkItemSaveReqVO reqVO) { chatRoleMapper.insert(null); return 1L; }\n"
                "}\n",
                encoding="utf-8",
            )
            (mod / "service/helper/RbacTouchServiceImpl.java").write_text(
                "public class RbacTouchServiceImpl implements RbacTouchService {\n"
                "  private RoleMapper roleMapper;\n"
                "  public void touch() { roleMapper.insert(null); }\n"
                "}\n",
                encoding="utf-8",
            )
            (mod / "service/item/WorkItemServiceImpl.java").write_text(
                "public class WorkItemServiceImpl implements WorkItemService {\n"
                "  private WorkItemMapper itemMapper;\n"
                "  private WorkHelperService helperService;\n"
                "  private CrmPermissionService permissionService;\n"
                "  private AsyncHelperService asyncHelperService;\n"
                "  private RbacTouchService rbacTouchService;\n"
                "  private PayClient payClient;\n"
                "  public Long createSafe(WorkItemSaveReqVO reqVO) { itemMapper.insert(null); helperService.writeLog(1L); permissionService.createPermission(1L); return 1L; }\n"
                "  public Long createAsync(WorkItemSaveReqVO reqVO) { itemMapper.insert(null); asyncHelperService.writeAsync(1L); return 1L; }\n"
                "  public Long createRbac(WorkItemSaveReqVO reqVO) { itemMapper.insert(null); rbacTouchService.touch(); return 1L; }\n"
                "  public Long createClient(WorkItemSaveReqVO reqVO) { itemMapper.insert(null); payClient.send(1L); return 1L; }\n"
                "}\n",
                encoding="utf-8",
            )
            for idx, (slug, cname, meth) in enumerate((
                ("work/safe", "WorkSafeController", "createSafe"),
                ("work/async", "WorkAsyncController", "createAsync"),
                ("work/rbac", "WorkRbacController", "createRbac"),
                ("work/client", "WorkClientController", "createClient"),
            )):
                (mod / f"controller/admin/item/{cname}.java").write_text(
                    f"package p;\n@RequestMapping(\"/{slug}\") @RestController public class {cname} {{\n"
                    f"  private WorkItemService workItemService;\n"
                    f"  @PostMapping(\"/create\") public CommonResult<Long> create(@RequestBody WorkItemSaveReqVO reqVO) {{ return success(workItemService.{meth}(reqVO)); }}\n"
                    f"}}\n",
                    encoding="utf-8",
                )
            (mod / "controller/admin/item/WorkChatRoleController.java").write_text(
                "package p;\n@RequestMapping(\"/work/chatrole\") @RestController public class WorkChatRoleController {\n"
                "  private AiChatRoleService aiChatRoleService;\n"
                "  @PostMapping(\"/create\") public CommonResult<Long> create(@RequestBody WorkItemSaveReqVO reqVO) { return success(aiChatRoleService.createChatRole(reqVO)); }\n"
                "}\n",
                encoding="utf-8",
            )
            paths = {}
            for slug in ("work/safe", "work/async", "work/rbac", "work/client", "work/chatrole"):
                paths[f"/admin-api/{slug}/get"] = {"get": {"responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/ItemResp"}}}}}}}
                paths[f"/admin-api/{slug}/create"] = {
                    "post": {
                        "tags": [f"管理后台 - {slug}"],
                        "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/WorkItemSaveReqVO"}}}},
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}},
                    }
                }
            docs = {
                "paths": paths,
                "components": {
                    "schemas": {
                        "CommonResultLong": {"type": "object", "properties": {"code": {"type": "integer"}, "data": {"type": "integer"}}},
                        "ItemResp": {"type": "object", "properties": {"data": {"type": "object", "properties": {"id": {"type": "integer"}, "name": {"type": "string"}}}}},
                        "WorkItemSaveReqVO": {"type": "object", "required": ["name"], "properties": {"id": {"type": "integer"}, "name": {"type": "string"}}},
                    }
                },
            }
            port = YudaoConventionWritePort(FakeYudaoClient(), FakeGoalData(docs, source=YudaoSource(troot, release="v-w0-2hop")))
            creates = port.create_specs()
            self.assertIn("work/safe", creates)
            self.assertIn("work/chatrole", creates)
            self.assertNotIn("work/async", creates)
            self.assertNotIn("work/rbac", creates)
            self.assertNotIn("work/client", creates)
            se_targets = {s.target for s in creates["work/safe"].side_effects}
            self.assertIn("work_log", se_targets)
            self.assertTrue(any("permission" in t for t in se_targets))

    def test_w0_baseline_companion_methods_and_approval_card_disclosure(self):
        from web_operation_agent.business.compiled_writes import CompiledCreateHandler

        base_safe_methods = (
            "createPermission",
            "lockAccountSet",
            "updateReceivablePlanReceivableId",
            "setAuxiliaryCombinationIds",
            "createSalaryChangeRecord",
            "updateSalaryChangeRecord",
            "updatePurchaseOrderInCount",
            "updatePurchaseOrderReturnCount",
            "updateSaleOrderOutCount",
            "updateSaleOrderReturnCount",
            "updatePurchaseInPaymentPrice",
            "updatePurchaseReturnRefundPrice",
            "updateSaleInReceiptPrice",
            "updateSaleReturnRefundPrice",
        )
        with tempfile.TemporaryDirectory() as tmp:
            troot = Path(tmp)
            _write_synthetic_release(troot)
            mod = troot / "server/yudao-module-work/src/main/java/cn/iocoder/yudao/module/work"
            (mod / "controller/admin/basecomp").mkdir(parents=True, exist_ok=True)
            (mod / "service/basecomp").mkdir(parents=True, exist_ok=True)
            (mod / "dal/dataobject/basecomp").mkdir(parents=True, exist_ok=True)
            (mod / "dal/dataobject/basecomp/BaseCompDO.java").write_text(
                "@TableName(\"base_comp\")\npublic class BaseCompDO { private Long id; private String name; }\n",
                encoding="utf-8",
            )
            svc_fields_lines = "\n".join(f"  private Comp{i}Service comp{i}Service;" for i in range(len(base_safe_methods)))
            svc_methods = []
            paths = {}
            for idx, mname in enumerate(base_safe_methods):
                slug = f"work/basecomp{idx}"
                (mod / f"controller/admin/basecomp/BaseComp{idx}Controller.java").write_text(
                    f"package p;\n@RequestMapping(\"/{slug}\") @RestController public class BaseComp{idx}Controller {{\n"
                    f"  private BaseCompService baseCompService;\n"
                    f"  @PostMapping(\"/create\") public CommonResult<Long> create(@RequestBody BaseCompSaveReqVO reqVO) {{ return success(baseCompService.create{idx}(reqVO)); }}\n"
                    f"}}\n",
                    encoding="utf-8",
                )
                svc_methods.append(
                    f"  public Long create{idx}(BaseCompSaveReqVO reqVO) {{ mapper.insert(null); comp{idx}Service.{mname}(1L); return 1L; }}\n"
                )
                paths[f"/admin-api/{slug}/get"] = {"get": {"responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/BaseResp"}}}}}}}
                paths[f"/admin-api/{slug}/create"] = {
                    "post": {
                        "tags": [f"管理后台 - {slug}"],
                        "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/BaseCompSaveReqVO"}}}},
                        "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}},
                    }
                }
            (mod / "service/basecomp/BaseCompServiceImpl.java").write_text(
                "public class BaseCompServiceImpl implements BaseCompService {\n"
                "  private BaseCompMapper mapper;\n"
                + svc_fields_lines + "\n" + "".join(svc_methods) + "}\n",
                encoding="utf-8",
            )
            docs = {
                "paths": paths,
                "components": {
                    "schemas": {
                        "CommonResultLong": {"type": "object", "properties": {"code": {"type": "integer"}, "data": {"type": "integer"}}},
                        "BaseResp": {"type": "object", "properties": {"data": {"type": "object", "properties": {"id": {"type": "integer"}, "name": {"type": "string"}}}}},
                        "BaseCompSaveReqVO": {"type": "object", "required": ["name"], "properties": {"id": {"type": "integer"}, "name": {"type": "string"}}},
                    }
                },
            }
            port = YudaoConventionWritePort(FakeYudaoClient(), FakeGoalData(docs, source=YudaoSource(troot, release="v-w0-base18")))
            creates = port.create_specs()
            for idx, mname in enumerate(base_safe_methods):
                slug = f"work/basecomp{idx}"
                self.assertIn(slug, creates, f"{mname} should remain admitted")
                self.assertTrue(creates[slug].side_effects, f"{mname} should disclose companion write")
            handler = CompiledCreateHandler(port)
            prop = handler.prepare({"entity": "work/basecomp0", "fields": [{"field": "name", "value": "测试单"}]}, context=_ctx())
            self.assertTrue(any(d.kind == "side_effect" and d.label.startswith("伴生写入") for d in prop.details))

    def test_w0_put_1n_projection_direct_id_list_and_unreadable_agg_guard(self):
        from web_operation_agent.business.convention_writes import ConventionUpdateHandler

        with tempfile.TemporaryDirectory() as tmp:
            troot = Path(tmp)
            _write_synthetic_release(troot)
            mod = troot / "server/yudao-module-oa/src/main/java/cn/iocoder/yudao/module/oa"
            for sub in ("controller/admin/task/vo", "service/task", "dal/dataobject/task"):
                (mod / sub).mkdir(parents=True, exist_ok=True)
            (mod / "dal/dataobject/task/OaTaskDO.java").write_text(
                "@TableName(\"oa_task\")\npublic class OaTaskDO { private Long id; private String title; }\n",
                encoding="utf-8",
            )
            (mod / "dal/dataobject/task/OaTaskReceiverDO.java").write_text(
                "@TableName(\"oa_task_receiver\")\npublic class OaTaskReceiverDO { private Long id; private Long taskId; private Long userId; }\n",
                encoding="utf-8",
            )
            (mod / "service/task/OaTaskServiceImpl.java").write_text(
                "public class OaTaskServiceImpl implements OaTaskService {\n"
                "  private OaTaskMapper taskMapper;\n"
                "  private OaTaskReceiverMapper taskReceiverMapper;\n"
                "  public void updateTask(OaTaskSaveReqVO reqVO) {\n"
                "    taskMapper.updateById(BeanUtils.toBean(reqVO, OaTaskDO.class));\n"
                "    taskReceiverMapper.insertBatch(reqVO.getReceiverUserIds());\n"
                "  }\n"
                "}\n",
                encoding="utf-8",
            )
            (mod / "controller/admin/task/OaTaskController.java").write_text(
                "package p;\n@RequestMapping(\"/oa/task\") @RestController public class OaTaskController {\n"
                "  private OaTaskService taskService;\n"
                "  @PutMapping(\"/update\") public CommonResult<Boolean> update(@RequestBody OaTaskSaveReqVO reqVO) { taskService.updateTask(reqVO); return success(true); }\n"
                "}\n",
                encoding="utf-8",
            )
            (mod / "controller/admin/task/BadAggController.java").write_text(
                "package p;\n@RequestMapping(\"/oa/badagg\") @RestController public class BadAggController {\n"
                "  @PutMapping(\"/update\") public CommonResult<Boolean> update(@RequestBody BadAggReqVO reqVO) { return success(true); }\n"
                "}\n",
                encoding="utf-8",
            )

            class ProjClient(FakeYudaoClient):
                def __init__(self):
                    super().__init__()
                    self.records[("oa/task", 77)] = {
                        "id": 77,
                        "title": "原任务",
                        "receivers": [{"id": 1, "userId": 9}],
                    }

                def get(self, path: str, **params):
                    if path == "system/user/simple-list":
                        return [{"id": 9, "nickname": "管理员"}, {"id": 10, "nickname": "李四"}]
                    return super().get(path, **params)

                def write_once(self, method: str, path: str, *, body: dict, params=None, operation: str = ""):
                    if path == "oa/task/update":
                        rec = self.records[("oa/task", body["id"])]
                        rec["title"] = body.get("title", rec["title"])
                        rec["receivers"] = [{"id": i + 1, "userId": uid} for i, uid in enumerate(body.get("receiverUserIds") or ())]
                        return True
                    return super().write_once(method, path, body=body, params=params, operation=operation)

            docs = {
                "paths": {
                    "/admin-api/system/user/simple-list": {"get": {"responses": {"200": {"content": {"*/*": {"schema": {"type": "array"}}}}}}},
                    "/admin-api/oa/task/get": {"get": {"responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/OaTaskResp"}}}}}}},
                    "/admin-api/oa/task/page": {
                        "get": {
                            "parameters": [{"name": "name", "in": "query"}, {"name": "pageNo", "in": "query"}, {"name": "pageSize", "in": "query"}],
                            "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/OaTaskPageResp"}}}}},
                        }
                    },
                    "/admin-api/oa/task/update": {
                        "put": {
                            "tags": ["管理后台 - OA 任务"],
                            "requestBody": {"content": {"application/json": {"schema": {
                                "type": "object", "required": ["id"],
                                "properties": {
                                    "id": {"type": "integer"},
                                    "title": {"type": "string"},
                                    "receiverUserIds": {"type": "array", "items": {"type": "integer"}},
                                },
                            }}}},
                            "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultBoolean"}}}}},
                        }
                    },
                    "/admin-api/oa/badagg/get": {"get": {"responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/BadAggResp"}}}}}}},
                    "/admin-api/oa/badagg/update": {
                        "put": {
                            "tags": ["管理后台 - 缺失回读子表"],
                            "requestBody": {"content": {"application/json": {"schema": {
                                "type": "object", "required": ["id"],
                                "properties": {
                                    "id": {"type": "integer"},
                                    "projects": {"type": "array", "items": {"$ref": "#/components/schemas/BadProjItem"}},
                                },
                            }}}},
                            "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultBoolean"}}}}},
                        }
                    },
                },
                "components": {
                    "schemas": {
                        "CommonResultBoolean": {"type": "object", "properties": {"code": {"type": "integer"}, "data": {"type": "boolean"}}},
                        "OaTaskReceiverItem": {"type": "object", "properties": {"id": {"type": "integer"}, "userId": {"type": "integer"}}},
                        "OaTaskResp": {"type": "object", "properties": {"data": {"type": "object", "properties": {
                            "id": {"type": "integer"}, "title": {"type": "string"},
                            "receivers": {"type": "array", "items": {"$ref": "#/components/schemas/OaTaskReceiverItem"}},
                        }}}},
                        "OaTaskPageResp": {"type": "object", "properties": {"data": {"type": "object", "properties": {
                            "list": {"type": "array", "items": {"$ref": "#/components/schemas/OaTaskResp"}}, "total": {"type": "integer"},
                        }}}},
                        "BadProjItem": {"type": "object", "properties": {"name": {"type": "string"}}},
                        "BadAggResp": {"type": "object", "properties": {"data": {"type": "object", "properties": {"id": {"type": "integer"}, "otherList": {"type": "array", "items": {"$ref": "#/components/schemas/BadProjItem"}}}}}},
                    }
                },
            }
            client = ProjClient()
            port = YudaoConventionWritePort(client, FakeGoalData(docs, source=YudaoSource(troot, release="v-w0-1n")))
            self.assertNotIn("oa/badagg", port.aggregate_update_specs())
            spec = port.update_specs()["oa/task"]
            self.assertEqual(spec.resolve_field("receiverUserIds").readback_projection, ("receivers", "userId"))
            self.assertEqual(spec.resolve_field("receiverUserIds").reference_entity, "system/user")
            self.assertTrue(any(s.target == "oa_task_receiver" for s in spec.side_effects))
            handler = ConventionUpdateHandler(port)
            prop = handler.prepare(
                {"entity": "oa/task", "target": "原任务（ID=77）", "changes": [{"field": "receiverUserIds", "value": ["李四"]}]},
                context=_ctx(),
            )
            self.assertEqual(prop.args["payload"]["receiverUserIds"], [10])
            self.assertTrue(any(d.kind == "side_effect" for d in prop.details))
            client.write_once("PUT", "oa/task/update", body=prop.args["payload"])
            after = client.get("oa/task/get", id=77)
            from web_operation_agent.business.convention_writes import extract_projected_field_value
            self.assertEqual(extract_projected_field_value(port, spec, spec.resolve_field("receiverUserIds"), after), [10])

    def test_w0_f2_dual_source_fk_agreement_contradiction_and_polymorphic(self):
        with tempfile.TemporaryDirectory() as tmp:
            troot = Path(tmp)
            _write_synthetic_release(troot)
            mod = troot / "server/yudao-module-demo/src/main/java/cn/iocoder/yudao/module/demo"
            (mod / "controller/admin/order").mkdir(parents=True, exist_ok=True)
            (mod / "service/order").mkdir(parents=True, exist_ok=True)
            (mod / "service/partner").mkdir(parents=True, exist_ok=True)
            (mod / "service/vendor").mkdir(parents=True, exist_ok=True)
            (mod / "service/partner/DemoPartnerServiceImpl.java").write_text(
                "public class DemoPartnerServiceImpl implements DemoPartnerService { public void validatePartnerExists(Long id) {} }\n",
                encoding="utf-8",
            )
            (mod / "service/vendor/DemoVendorServiceImpl.java").write_text(
                "public class DemoVendorServiceImpl implements DemoVendorService { public void validateVendorExists(Long id) {} }\n",
                encoding="utf-8",
            )
            (mod / "service/order/DemoOrderServiceImpl.java").write_text(
                "public class DemoOrderServiceImpl implements DemoOrderService {\n"
                "  private DemoOrderMapper orderMapper;\n"
                "  private DemoPartnerService partnerService;\n"
                "  private DemoVendorService vendorService;\n"
                "  public Long createOrder(DemoOrderSaveReqVO reqVO) {\n"
                "    partnerService.validatePartnerExists(reqVO.getClientPartnerId());\n"
                "    vendorService.validateVendorExists(reqVO.getMixedPartyId());\n"
                "    orderMapper.insert(null);\n"
                "    return 1L;\n"
                "  }\n"
                "}\n",
                encoding="utf-8",
            )
            (mod / "controller/admin/order/DemoPartnerController.java").write_text(
                "package p;\n@RequestMapping(\"/demo/partner\") @RestController public class DemoPartnerController {\n"
                "  private DemoPartnerService demoPartnerService;\n"
                "  @GetMapping(\"/simple-list\") public Object list() { return demoPartnerService.list(); }\n"
                "}\n",
                encoding="utf-8",
            )
            (mod / "controller/admin/order/DemoVendorController.java").write_text(
                "package p;\n@RequestMapping(\"/demo/vendor\") @RestController public class DemoVendorController {\n"
                "  private DemoVendorService demoVendorService;\n"
                "  @GetMapping(\"/simple-list\") public Object list() { return demoVendorService.list(); }\n"
                "}\n",
                encoding="utf-8",
            )
            (mod / "controller/admin/order/DemoOrderController.java").write_text(
                "package p;\n@RequestMapping(\"/demo/order\") @RestController public class DemoOrderController {\n"
                "  private DemoOrderService demoOrderService;\n"
                "  @PostMapping(\"/create\") public CommonResult<Long> create(@RequestBody DemoOrderSaveReqVO reqVO) { return success(demoOrderService.createOrder(reqVO)); }\n"
                "}\n",
                encoding="utf-8",
            )
            ui_api_dir = troot / "ui/src/api/demo"
            ui_view_dir = troot / "ui/src/views/demo/order"
            ui_api_dir.mkdir(parents=True, exist_ok=True)
            ui_view_dir.mkdir(parents=True, exist_ok=True)
            (ui_api_dir / "order.ts").write_text("export const createOrder = (data: any) => request.post({ url: '/demo/order/create', data })\n", encoding="utf-8")
            (ui_api_dir / "partner.ts").write_text("export const getPartnerSimpleList = () => request.get({ url: '/demo/partner/simple-list' })\n", encoding="utf-8")
            (ui_view_dir / "OrderForm.vue").write_text(
                "<template><el-form :model=\"formData\">"
                "<el-form-item label=\"伙伴\" prop=\"clientPartnerId\"><el-select v-model=\"formData.clientPartnerId\"><el-option v-for=\"p in partnerList\" :value=\"p.id\" /></el-select></el-form-item>"
                "<el-form-item label=\"矛盾\" prop=\"mixedPartyId\"><el-select v-model=\"formData.mixedPartyId\"><el-option v-for=\"m in mixedList\" :value=\"m.id\" /></el-select></el-form-item>"
                "</el-form></template>\n<script setup lang=\"ts\">\n"
                "import * as OrderApi from '@/api/demo/order'\n"
                "import * as PartnerApi from '@/api/demo/partner'\n"
                "const load = async () => { partnerList.value = await PartnerApi.getPartnerSimpleList(); mixedList.value = await PartnerApi.getPartnerSimpleList(); }\n"
                "const submit = async () => { await OrderApi.createOrder(formData.value) }\n"
                "</script>\n",
                encoding="utf-8",
            )
            docs = {
                "paths": {
                    "/admin-api/demo/partner/simple-list": {"get": {"responses": {"200": {"content": {"*/*": {"schema": {"type": "array"}}}}}}},
                    "/admin-api/demo/vendor/simple-list": {"get": {"responses": {"200": {"content": {"*/*": {"schema": {"type": "array"}}}}}}},
                    "/admin-api/demo/order/get": {"get": {"responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/OrderResp"}}}}}}},
                    "/admin-api/demo/order/create": {
                        "post": {
                            "tags": ["管理后台 - 测试订单"],
                            "requestBody": {"content": {"application/json": {"schema": {
                                "type": "object", "required": ["clientPartnerId"],
                                "properties": {
                                    "clientPartnerId": {"type": "integer"},
                                    "mixedPartyId": {"type": "integer"},
                                    "bizType": {"type": "integer"},
                                    "bizId": {"type": "integer"},
                                },
                            }}}},
                            "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}},
                        }
                    },
                },
                "components": {
                    "schemas": {
                        "CommonResultLong": {"type": "object", "properties": {"code": {"type": "integer"}, "data": {"type": "integer"}}},
                        "OrderResp": {"type": "object", "properties": {"data": {"type": "object", "properties": {
                            "id": {"type": "integer"}, "clientPartnerId": {"type": "integer"}, "mixedPartyId": {"type": "integer"},
                            "bizType": {"type": "integer"}, "bizId": {"type": "integer"},
                        }}}},
                    }
                },
            }
            port = YudaoConventionWritePort(FakeYudaoClient(), FakeGoalData(docs, source=YudaoSource(troot, release="v-w0-f2")))
            sp = port.create_specs()["demo/order"]
            self.assertEqual(sp.resolve_field("clientPartnerId").reference_entity, "demo/partner")
            self.assertEqual(sp.resolve_field("mixedPartyId").reference_entity, "")
            self.assertEqual(sp.resolve_field("bizId").reference_entity, "")

    def test_w0_f3_string_array_empty_list_default_and_encode(self):
        from web_operation_agent.business.compiled_writes import CompiledCreateHandler

        with tempfile.TemporaryDirectory() as tmp:
            troot = Path(tmp)
            _write_synthetic_release(troot)
            ui_api_dir = troot / "ui/src/api/oa"
            ui_view_dir = troot / "ui/src/views/oa/note"
            ui_api_dir.mkdir(parents=True, exist_ok=True)
            ui_view_dir.mkdir(parents=True, exist_ok=True)
            (ui_api_dir / "note.ts").write_text("export const createNote = (data: any) => request.post({ url: '/oa/note/create', data })\n", encoding="utf-8")
            (ui_view_dir / "NoteForm.vue").write_text(
                "<template><el-form :model=\"formData\"></el-form></template>\n<script setup lang=\"ts\">\n"
                "import * as NoteApi from '@/api/oa/note'\n"
                "const formData = ref<any>({\n  title: '',\n  fileUrls: [] as string[],\n})\n"
                "const handleAddRow = () => { const row = { tags: [] }; formData.value.items.push(row) }\n"
                "const submit = async () => { await NoteApi.createNote(formData.value) }\n"
                "</script>\n",
                encoding="utf-8",
            )
            docs = {
                "paths": {
                    "/admin-api/oa/note/get": {"get": {"responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/NoteResp"}}}}}}},
                    "/admin-api/oa/note/create": {
                        "post": {
                            "tags": ["管理后台 - 笔记"],
                            "requestBody": {"content": {"application/json": {"schema": {
                                "type": "object", "required": ["title", "fileUrls"],
                                "properties": {"title": {"type": "string"}, "fileUrls": {"type": "array", "items": {"type": "string"}}},
                            }}}},
                            "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}},
                        }
                    },
                },
                "components": {
                    "schemas": {
                        "CommonResultLong": {"type": "object", "properties": {"code": {"type": "integer"}, "data": {"type": "integer"}}},
                        "NoteResp": {"type": "object", "properties": {"data": {"type": "object", "properties": {
                            "id": {"type": "integer"}, "title": {"type": "string"}, "fileUrls": {"type": "array", "items": {"type": "string"}},
                        }}}},
                    }
                },
            }
            port = YudaoConventionWritePort(FakeYudaoClient(), FakeGoalData(docs, source=YudaoSource(troot, release="v-w0-f3")))
            sp = port.create_specs()["oa/note"]
            f_spec = sp.resolve_field("fileUrls")
            self.assertEqual(f_spec.kind, "string_array")
            d_spec = next(d for d in sp.defaults if d.field == "fileUrls")
            self.assertEqual(d_spec.value, [])
            self.assertEqual(d_spec.display, "[]（空列表）")
            val, _old, disp, _b = port.encode_field(sp, f_spec, ["https://a.test/1.png"], {"fileUrls": []})
            self.assertEqual(val, ["https://a.test/1.png"])
            self.assertIn("https://a.test/1.png", disp)
            with self.assertRaises(BusinessToolRequestError):
                port.encode_field(sp, f_spec, [123], {"fileUrls": []})
            handler = CompiledCreateHandler(port)
            prop = handler.prepare({"entity": "oa/note", "fields": [{"field": "title", "value": "随记"}]}, context=_ctx())
            self.assertEqual(prop.args["payload"]["fileUrls"], [])

    def test_w0_assert_true_rules_and_directory_line_formatting(self):
        from web_operation_agent_yudao.writes.convention_writes import build_yudao_write_directory
        from web_operation_agent_yudao.executor.domain_toolpacks import format_write_entry_line

        with tempfile.TemporaryDirectory() as tmp:
            troot = Path(tmp)
            _write_synthetic_release(troot)
            mod = troot / "server/yudao-module-oa/src/main/java/cn/iocoder/yudao/module/oa"
            (mod / "controller/admin/rep/vo").mkdir(parents=True, exist_ok=True)
            (mod / "dal/dataobject/rep").mkdir(parents=True, exist_ok=True)
            (mod / "controller/admin/rep/vo/OaRepSaveReqVO.java").write_text(
                "public class OaRepSaveReqVO {\n"
                "  private Long id;\n"
                "  private LocalDateTime startTime;\n"
                "  private LocalDateTime endTime;\n"
                "  private String summary;\n"
                "  private String plan;\n"
                "  private List<WorkItem> workItems;\n"
                "  @AssertTrue(message = \"结束时间不能早于开始时间\")\n"
                "  public boolean isTimeRangeValid() { return startTime == null || endTime == null || !startTime.isAfter(endTime); }\n"
                "  @AssertTrue(message = \"文字或明细至少填一项\")\n"
                "  public boolean isContentValid() { return StrUtil.isNotBlank(summary) || StrUtil.isNotBlank(plan) || CollUtil.isNotEmpty(workItems); }\n"
                "  public static class WorkItem { private String content; }\n"
                "}\n",
                encoding="utf-8",
            )
            (mod / "controller/admin/rep/vo/OaScalarPairSaveReqVO.java").write_text(
                "public class OaScalarPairSaveReqVO {\n"
                "  private Long id;\n"
                "  private String summary;\n"
                "  private String plan;\n"
                "  @AssertTrue(message = \"请至少填写总结或计划\")\n"
                "  public boolean isScalarValid() { return StrUtil.isNotBlank(summary) || StrUtil.isNotBlank(plan); }\n"
                "}\n",
                encoding="utf-8",
            )
            (mod / "service/rep").mkdir(parents=True, exist_ok=True)
            (mod / "service/rep/OaRepServiceImpl.java").write_text(
                "public class OaRepServiceImpl implements OaRepService {\n"
                "  private OaRepMapper mapper;\n"
                "  public Long createRep(OaRepSaveReqVO reqVO) { mapper.insert(null); return 1L; }\n"
                "  public Long createPair(OaScalarPairSaveReqVO reqVO) { mapper.insert(null); return 1L; }\n"
                "}\n",
                encoding="utf-8",
            )
            (mod / "controller/admin/rep/OaRepController.java").write_text(
                "package p;\n@RequestMapping(\"/oa/rep\") @RestController public class OaRepController {\n"
                "  private OaRepService oaRepService;\n"
                "  @PostMapping(\"/create\") public CommonResult<Long> create(@RequestBody OaRepSaveReqVO reqVO) { return success(oaRepService.createRep(reqVO)); }\n"
                "}\n",
                encoding="utf-8",
            )
            (mod / "controller/admin/rep/OaScalarPairController.java").write_text(
                "package p;\n@RequestMapping(\"/oa/scalarpair\") @RestController public class OaScalarPairController {\n"
                "  private OaRepService oaRepService;\n"
                "  @PostMapping(\"/create\") public CommonResult<Long> create(@RequestBody OaScalarPairSaveReqVO reqVO) { return success(oaRepService.createPair(reqVO)); }\n"
                "}\n",
                encoding="utf-8",
            )
            docs = {
                "paths": {
                    "/admin-api/oa/rep/get": {"get": {"responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/RepResp"}}}}}}},
                    "/admin-api/oa/rep/create": {
                        "post": {
                            "tags": ["管理后台 - 汇报"],
                            "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/OaRepSaveReqVO"}}}},
                            "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}},
                        }
                    },
                    "/admin-api/oa/scalarpair/get": {"get": {"responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/PairResp"}}}}}}},
                    "/admin-api/oa/scalarpair/create": {
                        "post": {
                            "tags": ["管理后台 - 二选一"],
                            "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/OaScalarPairSaveReqVO"}}}},
                            "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}},
                        }
                    },
                },
                "components": {
                    "schemas": {
                        "CommonResultLong": {"type": "object", "properties": {"code": {"type": "integer"}, "data": {"type": "integer"}}},
                        "WorkItem": {"type": "object", "properties": {"content": {"type": "string"}}},
                        "OaRepSaveReqVO": {"type": "object", "properties": {
                            "startTime": {"type": "integer"}, "endTime": {"type": "integer"},
                            "summary": {"type": "string"}, "plan": {"type": "string"},
                            "workItems": {"type": "array", "items": {"$ref": "#/components/schemas/WorkItem"}},
                            "timeRangeValid": {"type": "boolean"}, "contentValid": {"type": "boolean"},
                        }},
                        "RepResp": {"type": "object", "properties": {"data": {"type": "object", "properties": {
                            "id": {"type": "integer"}, "startTime": {"type": "integer"}, "endTime": {"type": "integer"},
                            "summary": {"type": "string"}, "plan": {"type": "string"},
                            "workItems": {"type": "array", "items": {"$ref": "#/components/schemas/WorkItem"}},
                        }}}},
                        "OaScalarPairSaveReqVO": {"type": "object", "properties": {
                            "summary": {"type": "string"}, "plan": {"type": "string"}, "scalarValid": {"type": "boolean"},
                        }},
                        "PairResp": {"type": "object", "properties": {"data": {"type": "object", "properties": {
                            "id": {"type": "integer"}, "summary": {"type": "string"}, "plan": {"type": "string"},
                        }}}},
                    }
                },
            }
            src = YudaoSource(troot, release="v-w0-vr")
            port = YudaoConventionWritePort(FakeYudaoClient(), FakeGoalData(docs, source=src))
            rep = port.create_specs()["oa/rep"]
            self.assertIsNone(rep.resolve_field("timeRangeValid"))
            self.assertIsNone(rep.resolve_field("contentValid"))
            self.assertEqual([r.kind for r in rep.validation_rules], ["date_order"])
            self.assertEqual(rep.validation_rules[0].fields, ("startTime", "endTime"))
            from web_operation_agent.business.convention_writes import evaluate_validation_rules
            self.assertEqual(
                evaluate_validation_rules(rep.validation_rules, {"startTime": 1000, "endTime": 2000}, rep.resolve_field),
                [],
            )
            self.assertTrue(
                evaluate_validation_rules(rep.validation_rules, {"startTime": 3000, "endTime": 2000}, rep.resolve_field)
            )
            pair = port.create_specs()["oa/scalarpair"]
            self.assertEqual([r.kind for r in pair.validation_rules], ["at_least_one"])
            entries = build_yudao_write_directory(docs=docs, source=src, ui=None)
            rep_entry = next(e for e in entries if e["entity"] == "oa/rep" and e["kind"] == "compiled_create")
            line = format_write_entry_line(rep_entry)
            self.assertIn("校验:", line)
            self.assertIn("结束时间不能早于开始时间", line)

    def test_w0_tree_cell_values_tree_ref_lookup_and_area_variants(self):
        from web_operation_agent_yudao.executor.actor_knowledge import _cell_values
        from web_operation_agent.business.convention_writes import ConventionFieldSpec

        tree_rows = [
            {
                "id": 10,
                "name": "总部",
                "parentId": 0,
                "children": [
                    {"id": 20, "name": "华东", "parentId": 10},
                ],
            },
            {
                "id": 30,
                "name": "分部",
                "parentId": 0,
                "children": [
                    {"id": 40, "name": "华东", "parentId": 30},
                ],
            },
        ]
        cands, _bound = _cell_values(tree_rows, entity_slug="mes/md-workshop")
        vals = {v for _k, v in cands}
        self.assertIn("总部/华东", vals)
        self.assertIn("分部/华东", vals)

        class TreeRefClient(FakeYudaoClient):
            def get(self, path: str, **params):
                if path == "mes/md-workshop/tree":
                    return tree_rows
                return super().get(path, **params)

        with tempfile.TemporaryDirectory() as tmp:
            troot = Path(tmp)
            _write_synthetic_release(troot)
            docs = {
                "paths": {
                    "/admin-api/mes/md-workshop/tree": {"get": {"responses": {"200": {"content": {"*/*": {"schema": {"type": "array"}}}}}}},
                },
                "components": {"schemas": {}},
            }
            port = YudaoConventionWritePort(TreeRefClient(), FakeGoalData(docs, source=YudaoSource(troot, release="v-w0-tree")))
            rid, lbl, _b = port._resolve_single_ref("mes/md-workshop", "分部/华东")
            self.assertEqual(rid, 40)
            self.assertEqual(lbl, "华东")
            area_field = ConventionFieldSpec(name="areaId", kind="integer", label="区域", reference_entity="system/area")
            variants = port.reference_provenance_variants(area_field, "浙江省/杭州市")
            self.assertIn("浙江", variants)
            self.assertIn("浙江省", variants)
            self.assertIn("杭州市", variants)

    def test_w0_verify_companion_readback_outcomes(self):
        from web_operation_agent.business.errors import BusinessMutationIndeterminate
        from web_operation_agent_yudao.reads.client import YudaoReadRefused

        class ReadbackClient(FakeYudaoClient):
            def __init__(self, mode: str):
                super().__init__()
                self.mode = mode

            def get(self, path: str, **params):
                if path == "crm/contact/list":
                    if self.mode == "ok":
                        return [{"id": 501, "businessId": params.get("businessId")}]
                    if self.mode == "empty":
                        return []
                    if self.mode == "403":
                        raise YudaoReadRefused("无权限", code=403)
                    if self.mode == "500":
                        raise YudaoReadRefused("服务器内部错误", code=500)
                return super().get(path, **params)

        with tempfile.TemporaryDirectory() as tmp:
            troot = Path(tmp)
            _write_synthetic_release(troot)
            docs = {
                "paths": {
                    "/admin-api/crm/contact/list": {"get": {"responses": {"200": {"content": {"*/*": {"schema": {"type": "array"}}}}}}},
                },
                "components": {"schemas": {}},
            }
            spec = NS(entity="crm/business")
            uncond_se = [{"target": "crm_contact_business", "action": "新增伴生记录（insertBatch）", "readback_entity": "crm/contact", "readback_query_field": "businessId", "condition": ""}]
            cond_se = [{"target": "crm_contact_business", "action": "新增伴生记录（insertBatch）", "readback_entity": "crm/contact", "readback_query_field": "businessId", "condition": "按条件触发"}]
            no_ep_se = [{"target": "work_log", "action": "新增伴生记录（insert）", "readback_entity": "", "readback_query_field": "", "condition": ""}]

            src = YudaoSource(troot, release="v-w0-rb")
            # 1. verified=True
            port_ok = YudaoConventionWritePort(ReadbackClient("ok"), FakeGoalData(docs, source=src))
            res_ok = port_ok.verify_companion_readback(spec, 88, {}, before_record=None, after_record={"id": 88}, side_effects=uncond_se)
            self.assertTrue(res_ok[0]["verified"])
            # 2. unconditional insert empty -> BusinessMutationIndeterminate
            port_empty = YudaoConventionWritePort(ReadbackClient("empty"), FakeGoalData(docs, source=src))
            with self.assertRaises(BusinessMutationIndeterminate):
                port_empty.verify_companion_readback(spec, 88, {}, before_record=None, after_record={"id": 88}, side_effects=uncond_se)
            # 3. conditional / update companion returning 0 rows -> verified=False with honest note (W0 §6 Item 4)
            res_cond = port_empty.verify_companion_readback(spec, 88, {}, before_record=None, after_record={"id": 88}, side_effects=cond_se)
            self.assertFalse(res_cond[0]["verified"])
            self.assertIn("写后未核验", res_cond[0]["note"])
            # 4. 500 error -> BusinessMutationIndeterminate
            port_500 = YudaoConventionWritePort(ReadbackClient("500"), FakeGoalData(docs, source=src))
            with self.assertRaises(BusinessMutationIndeterminate):
                port_500.verify_companion_readback(spec, 88, {}, before_record=None, after_record={"id": 88}, side_effects=uncond_se)
            # 5. 403 -> verified=False with honest note
            port_403 = YudaoConventionWritePort(ReadbackClient("403"), FakeGoalData(docs, source=src))
            res_403 = port_403.verify_companion_readback(spec, 88, {}, before_record=None, after_record={"id": 88}, side_effects=uncond_se)
            self.assertFalse(res_403[0]["verified"])
            self.assertIn("写后未核验", res_403[0]["note"])
            # 6. No read endpoint -> verified=False with honest note
            res_noep = port_ok.verify_companion_readback(spec, 88, {}, before_record=None, after_record={"id": 88}, side_effects=no_ep_se)
            self.assertFalse(res_noep[0]["verified"])
            self.assertIn("写后未核验", res_noep[0]["note"])

    def test_w0_r2_salary_id_volatile_resolve_target_and_project_ref(self):
        class W0R2Client(FakeYudaoClient):
            tenant = "1"

            def __init__(self):
                super().__init__()
                self.next_sal_pk = 900

            def get(self, path: str, **params):
                if path == "hrm/employee/page":
                    return {"list": [{"id": 63, "name": "于振宁"}]}
                if path == "hrm/salary/employee-info/page":
                    return {"list": [{"id": self.next_sal_pk, "employeeId": 63, "employeeName": "于振宁"}]}
                if path == "hrm/salary/employee-info/get":
                    return {
                        "id": self.next_sal_pk,
                        "employeeId": int(params["employeeId"]),
                        "employeeName": "于振宁",
                        "salaryOptions": [{"code": 10101, "name": "基本工资", "value": 8000}],
                    }
                if path == "fms/config/account-set/simple-list":
                    return [{"id": 7, "name": "主账套"}]
                if path == "fms/config/finance-indicator/list":
                    assert params.get("accountSetId") == 7
                    return [{"id": 11, "accountSetId": 7, "name": "现金净额", "code": "cash"}]
                if path == "fms/config/finance-indicator/get":
                    return {"id": int(params["id"]), "accountSetId": 7, "name": "现金净额", "code": "cash"}
                if path == "oa/task/published-page":
                    return {"list": [{"id": 21, "title": "盘点清单"}]}
                if path == "oa/task/received-page":
                    return {"list": []}
                if path == "oa/task/get":
                    return {"id": 21, "title": "盘点清单"}
                if path == "product/property/value/page":
                    st = params.get("status")
                    if st == 0:
                        return {"list": [{"id": 31, "name": "红色", "status": 0}]}
                    return {"list": []}
                if path == "product/property/value/get":
                    return {"id": 31, "name": "红色", "status": 0}
                if path == "mes/qc/defect-record/get":
                    return {"id": 1, "name": "表面划伤", "qcType": 1, "qcId": 10}
                if path == "pms/pm/project/page":
                    return {"list": [{"id": 5, "name": "研发项目A"}]}
                if path == "pms/pm/work-item/page":
                    if params.get("projectId") == 5:
                        return {"list": [{"id": 88, "name": "登录联调"}]}
                    return {"list": []}
                return super().get(path, **params)

            def write_once(self, method: str, path: str, *, body: dict, params=None, operation: str = "", expect_result: str = ""):
                if path == "hrm/salary/employee-info/update":
                    self.writes.append((method, path, copy.deepcopy(body)))
                    self.next_sal_pk += 1
                    return 701
                return True

        with tempfile.TemporaryDirectory() as tmp:
            troot = Path(tmp)
            _write_synthetic_release(troot)
            hrm_ctrl = troot / "server/yudao-module-hrm/src/main/java/cn/iocoder/yudao/module/hrm/controller/admin/salary"
            hrm_svc = troot / "server/yudao-module-hrm/src/main/java/cn/iocoder/yudao/module/hrm/service/salary"
            hrm_ctrl.mkdir(parents=True, exist_ok=True)
            hrm_svc.mkdir(parents=True, exist_ok=True)
            (hrm_svc / "HrmSalaryEmployeeInfoServiceImpl.java").write_text(
                "public class HrmSalaryEmployeeInfoServiceImpl { public Long updateEmployeeSalary(HrmSalReq req) { changeRecordMapper.insert(null); return 701L; } }\n",
                encoding="utf-8",
            )
            docs = {
                "paths": {
                    "/admin-api/hrm/salary/employee-info/page": {"get": {"responses": {"200": {"content": {"*/*": {"schema": {"type": "object"}}}}}}},
                    "/admin-api/hrm/salary/employee-info/get": {"get": {"parameters": [{"name": "employeeId", "in": "query", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/SalGet"}}}}}}},
                    "/admin-api/hrm/salary/employee-info/update": {"put": {"tags": ["员工薪资"], "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/SalReq"}}}}, "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}}}},
                    "/admin-api/hrm/salary/change-record/get": {"get": {"parameters": [{"name": "id", "in": "query", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/ChgGet"}}}}}}},
                    "/admin-api/fms/config/account-set/simple-list": {"get": {"responses": {"200": {"content": {"*/*": {"schema": {"type": "array"}}}}}}},
                    "/admin-api/fms/config/finance-indicator/list": {"get": {"parameters": [{"name": "accountSetId", "in": "query", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"content": {"*/*": {"schema": {"type": "array"}}}}}}},
                    "/admin-api/fms/config/finance-indicator/get": {"get": {"parameters": [{"name": "accountSetId", "in": "query", "required": True, "schema": {"type": "integer"}}, {"name": "id", "in": "query", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/NamedGet"}}}}}}},
                    "/admin-api/fms/config/finance-indicator/update": {"put": {"tags": ["财务指标"], "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/NamedUpd"}}}}, "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultBoolean"}}}}}}},
                    "/admin-api/oa/task/published-page": {"get": {"parameters": [], "responses": {"200": {"content": {"*/*": {"schema": {"type": "object"}}}}}}},
                    "/admin-api/oa/task/received-page": {"get": {"parameters": [], "responses": {"200": {"content": {"*/*": {"schema": {"type": "object"}}}}}}},
                    "/admin-api/oa/task/get": {"get": {"parameters": [{"name": "id", "in": "query", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/NamedGet"}}}}}}},
                    "/admin-api/oa/task/update": {"put": {"tags": ["OA任务"], "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/NamedUpd"}}}}, "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultBoolean"}}}}}}},
                    "/admin-api/product/property/value/page": {"get": {"parameters": [{"name": "status", "in": "query", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"content": {"*/*": {"schema": {"type": "object"}}}}}}},
                    "/admin-api/product/property/value/get": {"get": {"parameters": [{"name": "id", "in": "query", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/NamedGet"}}}}}}},
                    "/admin-api/product/property/value/update": {"put": {"tags": ["属性值"], "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/NamedUpd"}}}}, "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultBoolean"}}}}}}},
                    "/admin-api/mes/qc/defect-record/page": {"get": {"parameters": [{"name": "qcType", "in": "query", "required": True, "schema": {"type": "integer"}}, {"name": "qcId", "in": "query", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"content": {"*/*": {"schema": {"type": "object"}}}}}}},
                    "/admin-api/mes/qc/defect-record/get": {"get": {"parameters": [{"name": "id", "in": "query", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/NamedGet"}}}}}}},
                    "/admin-api/mes/qc/defect-record/update": {"put": {"tags": ["缺陷记录"], "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/NamedUpd"}}}}, "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultBoolean"}}}}}}},
                    "/admin-api/pms/pm/project/page": {"get": {"parameters": [], "responses": {"200": {"content": {"*/*": {"schema": {"type": "object"}}}}}}},
                    "/admin-api/pms/pm/work-item/page": {"get": {"parameters": [{"name": "projectId", "in": "query", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"content": {"*/*": {"schema": {"type": "object"}}}}}}},
                },
                "components": {
                    "schemas": {
                        "CommonResultBoolean": {"type": "object", "properties": {"code": {"type": "integer"}, "data": {"type": "boolean"}}},
                        "CommonResultLong": {"type": "object", "properties": {"code": {"type": "integer"}, "data": {"type": "integer"}}},
                        "OptItem": {"type": "object", "properties": {"code": {"type": "integer"}, "name": {"type": "string"}, "value": {"type": "number"}}},
                        "SalReq": {"type": "object", "required": ["employeeId"], "properties": {"employeeId": {"type": "integer"}, "salaryOptions": {"type": "array", "items": {"$ref": "#/components/schemas/OptItem"}}}},
                        "SalGet": {"type": "object", "properties": {"data": {"type": "object", "properties": {"id": {"type": "integer"}, "employeeId": {"type": "integer"}, "employeeName": {"type": "string"}, "salaryOptions": {"type": "array", "items": {"$ref": "#/components/schemas/OptItem"}}}}}},
                        "ChgGet": {"type": "object", "properties": {"data": {"type": "object", "properties": {"id": {"type": "integer"}, "employeeId": {"type": "integer"}}}}},
                        "NamedUpd": {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}, "name": {"type": "string"}}},
                        "NamedGet": {"type": "object", "properties": {"data": {"type": "object", "properties": {"id": {"type": "integer"}, "name": {"type": "string"}, "title": {"type": "string"}}}}},
                    }
                },
            }
            port = YudaoConventionWritePort(W0R2Client(), FakeGoalData(docs, source=YudaoSource(troot, release="v-w0-r2")))
            sal_spec = port.aggregate_update_specs()["hrm/salary/employee-info"]
            self.assertIn("id", sal_spec.volatile_fields)
            # Preservation snapshot check ignores changed internal PK `id` on employee-info
            from web_operation_agent.business.compiled_writes import verify_preservation_snapshot
            from web_operation_agent.business.errors import BusinessToolReferenceError
            before_sal = port.read_record("hrm/salary/employee-info", 63, spec=sal_spec)
            after_sal = port.read_record("hrm/salary/employee-info", 63, spec=sal_spec)
            after_sal["id"] = before_sal["id"] + 1
            self.assertEqual(
                verify_preservation_snapshot(
                    before_sal,
                    after_sal,
                    volatile=sal_spec.volatile_fields,
                    ignore=frozenset({sal_spec.id_field}) | sal_spec.server_computed_fields,
                ),
                [],
            )

            # resolve_target across accountSetId /list, /published-page, status-required /page, and candidate-ref GET fallback
            dummy_spec = NS(id_field="id", label="对象", name_fields=("name", "title"), composite_id_fields=())
            self.assertEqual(port.resolve_target("fms/config/finance-indicator", "现金净额（ID=11）", spec=dummy_spec)["id"], 11)
            self.assertEqual(port.resolve_target("oa/task", "盘点清单（ID=21）", spec=dummy_spec)["id"], 21)
            self.assertEqual(port.resolve_target("product/property/value", "红色（ID=31）", spec=dummy_spec)["id"], 31)
            self.assertEqual(port.resolve_target("mes/qc/defect-record", "表面划伤（ID=1）", spec=dummy_spec)["id"], 1)
            with self.assertRaises(BusinessToolReferenceError):
                port.resolve_target("mes/qc/defect-record", "错误名称（ID=1）", spec=dummy_spec)
            with self.assertRaises(BusinessToolRequestError):
                port.resolve_target("mes/qc/defect-record", "1", spec=dummy_spec)

            # _list_ref_rows fans out across pms/pm/project/page when reference endpoint requires projectId
            _, _, wi_rows, _ = port._list_ref_rows("pms/pm/work-item")
            self.assertEqual([r["id"] for r in wi_rows], [88])

    def test_w0_r3_child_derived_volatile_write_only_transform_and_project_refused_fallback(self):
        from web_operation_agent.business.errors import BusinessToolRequestError
        from web_operation_agent_yudao.reads.client import YudaoReadRefused

        class R3Client(FakeYudaoClient):
            tenant = "1"

            def get(self, path: str, **params):
                if path == "pms/pm/project/page":
                    return {"list": [{"id": 9, "name": "核心项目"}]}
                if path == "pms/pm/work-item/page":
                    if "projectId" not in params:
                        raise YudaoReadRefused("项目编号不能为空", code=400)
                    return {"list": [{"id": 91, "name": "子工作项"}]} if params["projectId"] == 9 else {"list": []}
                return super().get(path, **params)

        with tempfile.TemporaryDirectory() as tmp:
            troot = Path(tmp)
            _write_synthetic_release(troot)
            oa_ctrl = troot / "server/yudao-module-oa/src/main/java/cn/iocoder/yudao/module/oa/controller/admin/task/vo"
            oa_svc = troot / "server/yudao-module-oa/src/main/java/cn/iocoder/yudao/module/oa/service/task"
            fms_ctrl = troot / "server/yudao-module-fms/src/main/java/cn/iocoder/yudao/module/fms/controller/admin/config"
            fms_svc = troot / "server/yudao-module-fms/src/main/java/cn/iocoder/yudao/module/fms/service/config"
            for d in (oa_ctrl, oa_svc, fms_ctrl, fms_svc):
                d.mkdir(parents=True, exist_ok=True)
            fms_vo = fms_ctrl / "vo"
            fms_do = troot / "server/yudao-module-fms/src/main/java/cn/iocoder/yudao/module/fms/dal/dataobject/config"
            oa_vo = oa_ctrl / "vo"
            for d in (fms_vo, fms_do, oa_vo):
                d.mkdir(parents=True, exist_ok=True)
            (oa_svc / "OaTaskService.java").write_text(
                "public interface OaTaskService { void updateTask(OaTaskSaveReqVO reqVO); }\n",
                encoding="utf-8",
            )
            (oa_svc / "OaTaskServiceImpl.java").write_text(
                "public class OaTaskServiceImpl implements OaTaskService {\n"
                "  private OaTaskMapper taskMapper;\n"
                "  private OaTaskReceiverMapper taskReceiverMapper;\n"
                "  public void updateTask(OaTaskSaveReqVO reqVO) {\n"
                "    taskMapper.updateById(BeanUtils.toBean(reqVO, OaTaskDO.class));\n"
                "    List<List<Long>> diff = diffList(oldIds, reqVO.getReceiverUserIds(), ObjUtil::equal);\n"
                "    if (CollUtil.isNotEmpty(diff.get(2))) {\n"
                "      taskReceiverMapper.deleteByTaskIdAndUserIds(reqVO.getId(), diff.get(2));\n"
                "    }\n"
                "    taskReceiverMapper.insertBatch(null);\n"
                "  }\n"
                "}\n",
                encoding="utf-8",
            )
            (oa_ctrl / "OaTaskController.java").write_text(
                '@RequestMapping("/oa/task")\n'
                "public class OaTaskController {\n"
                "  private OaTaskService taskService;\n"
                '  @PutMapping("/update") public CommonResult<Boolean> update(@RequestBody OaTaskSaveReqVO reqVO) { taskService.updateTask(reqVO); return success(true); }\n'
                "}\n",
                encoding="utf-8",
            )
            (fms_do / "FmsFinanceIndicatorDO.java").write_text(
                '@TableName("fms_finance_indicator")\n'
                "public class FmsFinanceIndicatorDO { private Long id; private String name; private String formula; }\n",
                encoding="utf-8",
            )
            (fms_vo / "FmsFinReqVO.java").write_text(
                "public class FmsFinReqVO { private String name; private String formula; }\n",
                encoding="utf-8",
            )
            (fms_svc / "FmsFinanceIndicatorService.java").write_text(
                "public interface FmsFinanceIndicatorService { Long createFinanceIndicator(FmsFinReqVO reqVO); }\n",
                encoding="utf-8",
            )
            (fms_svc / "FmsFinanceIndicatorServiceImpl.java").write_text(
                "public class FmsFinanceIndicatorServiceImpl implements FmsFinanceIndicatorService {\n"
                "  private FmsFinanceIndicatorMapper indicatorMapper;\n"
                "  private String normalizeFormula(String f) { return f; }\n"
                "  private String bindFormula(String f) { return f; }\n"
                "  public Long createFinanceIndicator(FmsFinReqVO reqVO) {\n"
                "    FmsFinanceIndicatorDO entity = BeanUtils.toBean(reqVO, FmsFinanceIndicatorDO.class).setId(null)\n"
                "      .setFormula(bindFormula(normalizeFormula(reqVO.getFormula())));\n"
                "    indicatorMapper.insert(entity);\n"
                "    return entity.getId();\n"
                "  }\n"
                "}\n",
                encoding="utf-8",
            )
            (fms_ctrl / "FmsFinanceIndicatorController.java").write_text(
                '@RequestMapping("/fms/config/finance-indicator")\n'
                "public class FmsFinanceIndicatorController {\n"
                "  private FmsFinanceIndicatorService indicatorService;\n"
                '  @PostMapping("/create") public CommonResult<Long> create(@RequestBody FmsFinReqVO reqVO) { return success(indicatorService.createFinanceIndicator(reqVO)); }\n'
                "}\n",
                encoding="utf-8",
            )
            docs = {
                "paths": {
                    "/admin-api/oa/task/get": {"get": {"parameters": [{"name": "id", "in": "query", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/OaResp"}}}}}}},
                    "/admin-api/oa/task/update": {"put": {"tags": ["OA任务"], "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/OaReq"}}}}, "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultBoolean"}}}}}}},
                    "/admin-api/fms/config/finance-indicator/get": {"get": {"parameters": [{"name": "id", "in": "query", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/FinResp"}}}}}}},
                    "/admin-api/fms/config/finance-indicator/create": {"post": {"tags": ["财务指标"], "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/FinReq"}}}}, "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}}}},
                    "/admin-api/pms/pm/project/page": {"get": {"parameters": [], "responses": {"200": {"content": {"*/*": {"schema": {"type": "object"}}}}}}},
                    "/admin-api/pms/pm/work-item/page": {"get": {"parameters": [{"name": "projectId", "in": "query", "required": False, "schema": {"type": "integer"}}], "responses": {"200": {"content": {"*/*": {"schema": {"type": "object"}}}}}}},
                },
                "components": {
                    "schemas": {
                        "CommonResultBoolean": {"type": "object", "properties": {"code": {"type": "integer"}, "data": {"type": "boolean"}}},
                        "CommonResultLong": {"type": "object", "properties": {"code": {"type": "integer"}, "data": {"type": "integer"}}},
                        "OaRec": {"type": "object", "properties": {"userId": {"type": "integer"}}},
                        "OaReq": {"type": "object", "required": ["id", "title"], "properties": {"id": {"type": "integer"}, "title": {"type": "string"}, "receiverUserIds": {"type": "array", "items": {"type": "integer"}}}},
                        "OaResp": {"type": "object", "properties": {"data": {"type": "object", "properties": {"id": {"type": "integer"}, "title": {"type": "string"}, "receiverStatus": {"type": "integer"}, "receivers": {"type": "array", "items": {"$ref": "#/components/schemas/OaRec"}}}}}},
                        "FinReq": {"type": "object", "required": ["name", "formula"], "properties": {"name": {"type": "string"}, "formula": {"type": "string"}}},
                        "FinResp": {"type": "object", "properties": {"data": {"type": "object", "properties": {"id": {"type": "integer"}, "name": {"type": "string"}, "formula": {"type": "string"}}}}},
                    }
                },
            }
            port = YudaoConventionWritePort(R3Client(), FakeGoalData(docs, source=YudaoSource(troot, release="v-w0-r3")))
            oa_upd = port.update_specs()["oa/task"]
            self.assertIn("receiverStatus", oa_upd.volatile_fields)
            self.assertNotIn("title", oa_upd.volatile_fields)

            fin_create = port.create_specs()["fms/config/finance-indicator"]
            self.assertIn("formula", fin_create.write_only_fields)
            self.assertTrue(fin_create.resolve_field("formula").required)

            _, _, wi_rows, _ = port._list_ref_rows("pms/pm/work-item")
            self.assertEqual([r["id"] for r in wi_rows], [91])

    def test_w0_r4_recheck_bindings_project_scope_refusal_and_empty_simple_list_fallback(self):
        from web_operation_agent.business.errors import BusinessMutationRefusedError
        from web_operation_agent_yudao.reads.client import YudaoReadRefused

        class R4Client(FakeYudaoClient):
            tenant = "1"

            def __init__(self):
                super().__init__()
                self.fail_recheck = False

            def get(self, path: str, **params):
                if path == "pms/pm/project/page":
                    return {"list": [{"id": 12, "name": "交付项目"}]}
                if path == "pms/pm/work-item/page":
                    if self.fail_recheck or "projectId" not in params:
                        raise YudaoReadRefused("项目编号不能为空", code=400)
                    return {"list": [{"id": 301, "name": "联调任务"}]} if params["projectId"] == 12 else {"list": []}
                if path == "mes/pro/route/simple-list":
                    return []
                if path == "mes/pro/route/page":
                    return {"list": [{"id": 6, "name": "标准路线", "status": 1}]}
                return super().get(path, **params)

        with tempfile.TemporaryDirectory() as tmp:
            troot = Path(tmp)
            _write_synthetic_release(troot)
            docs = {
                "paths": {
                    "/admin-api/pms/pm/project/page": {"get": {"parameters": [], "responses": {"200": {"content": {"*/*": {"schema": {"type": "object"}}}}}}},
                    "/admin-api/pms/pm/work-item/page": {"get": {"parameters": [{"name": "projectId", "in": "query", "required": False, "schema": {"type": "integer"}}], "responses": {"200": {"content": {"*/*": {"schema": {"type": "object"}}}}}}},
                    "/admin-api/mes/pro/route/simple-list": {"get": {"parameters": [], "responses": {"200": {"content": {"*/*": {"schema": {"type": "array"}}}}}}},
                    "/admin-api/mes/pro/route/page": {"get": {"parameters": [{"name": "pageNo", "in": "query", "required": False, "schema": {"type": "integer"}}, {"name": "pageSize", "in": "query", "required": False, "schema": {"type": "integer"}}], "responses": {"200": {"content": {"*/*": {"schema": {"type": "object"}}}}}}},
                },
                "components": {"schemas": {}},
            }
            client = R4Client()
            port = YudaoConventionWritePort(client, FakeGoalData(docs, source=YudaoSource(troot, release="v-w0-r4")))

            wi_id, wi_lbl, wi_bind = port._resolve_single_ref("pms/pm/work-item", "联调任务（ID=301）")
            self.assertEqual(wi_id, 301)
            self.assertEqual(wi_lbl, "联调任务")
            self.assertEqual(wi_bind["query_params"].get("projectId"), 12)
            port.recheck_bindings([wi_bind], actor_id="1")

            client.fail_recheck = True
            with self.assertRaises(BusinessMutationRefusedError):
                port.recheck_bindings([wi_bind], actor_id="1")

            r_list_path, _, routes, r_qp = port._list_ref_rows("mes/pro/route")
            self.assertEqual(r_list_path, "mes/pro/route/page")
            self.assertEqual([r["id"] for r in routes], [6])
            self.assertEqual(r_qp, {"pageNo": 1, "pageSize": 100})
            rt_id, rt_lbl, rt_bind = port._resolve_single_ref("mes/pro/route", "标准路线（ID=6）")
            self.assertEqual((rt_id, rt_lbl, rt_bind["list_path"]), (6, "标准路线", "mes/pro/route/page"))
            port.recheck_bindings([rt_bind], actor_id="1")

    def test_s1_step2_compiler_exclusions_destructive_delete_derived_display_and_readback(self):
        from web_operation_agent.business.compiled_writes import CompiledCreateHandler
        from web_operation_agent.business.convention_writes import (
            ConventionUpdateHandler,
            WRITE_PORT_OPTIONAL,
            write_capabilities_of,
        )
        from web_operation_agent.business.errors import BusinessMutationIndeterminate, BusinessMutationRefusedError, BusinessToolRequestError
        from web_operation_agent.business.tools.contracts import BusinessMutationProposal

        with tempfile.TemporaryDirectory() as tmp:
            troot = Path(tmp)
            _write_synthetic_release(troot)
            mod = troot / "server/yudao-module-demo/src/main/java/cn/iocoder/yudao/module/demo"
            for sub in (
                "controller/admin/cfg", "controller/admin/card", "controller/admin/rule", "controller/admin/ruleline",
                "controller/admin/tpl", "controller/admin/tplpage", "controller/admin/seal", "controller/admin/wh",
                "controller/admin/plan", "controller/admin/pay", "controller/admin/schedule",
                "service/cfg", "service/card", "service/rule", "service/ruleline",
                "service/tpl", "service/tplpage", "service/seal", "service/wh",
                "service/plan", "service/pay", "service/schedule",
            ):
                (mod / sub).mkdir(parents=True, exist_ok=True)

            # 1) W0 §6 Item 1: singleton validateNotExists excluded; non-tx cross-FK confirmed + companion excluded
            (mod / "controller/admin/cfg/DemoCfgController.java").write_text(
                "@RequestMapping(\"/demo/cfg\")\npublic class DemoCfgController {\n"
                "  private DemoCfgService cfgService;\n"
                "  @PostMapping(\"/create\") public CommonResult<Long> createCfg(@RequestBody DemoCfgReqVO reqVO) { return success(cfgService.createCfg(reqVO)); }\n"
                "  @GetMapping(\"/get\") public CommonResult<DemoCfgRespVO> getCfg() { return null; }\n"
                "}\n", encoding="utf-8",
            )
            (mod / "service/cfg/DemoCfgServiceImpl.java").write_text(
                "public class DemoCfgServiceImpl implements DemoCfgService {\n"
                "  private DemoCfgMapper cfgMapper;\n"
                "  public Long createCfg(DemoCfgReqVO reqVO) {\n"
                "    validateCfgNotExists();\n"
                "    cfgMapper.insert(BeanUtils.toBean(reqVO, DemoCfgDO.class));\n"
                "    return 1L;\n"
                "  }\n"
                "  private void validateCfgNotExists() {}\n"
                "}\n", encoding="utf-8",
            )
            (mod / "controller/admin/card/DemoCardController.java").write_text(
                "@RequestMapping(\"/demo/card\")\npublic class DemoCardController {\n"
                "  private DemoCardService cardService;\n"
                "  @PostMapping(\"/create\") public CommonResult<Long> createCard(@RequestBody DemoCardReqVO reqVO) { return success(cardService.createCard(reqVO)); }\n"
                "  @GetMapping(\"/get\") public CommonResult<DemoCardRespVO> getCard(@RequestParam(\"id\") Long id) { return null; }\n"
                "}\n", encoding="utf-8",
            )
            (mod / "service/card/DemoCardServiceImpl.java").write_text(
                "public class DemoCardServiceImpl implements DemoCardService {\n"
                "  private DemoCardMapper cardMapper;\n"
                "  private DemoOrderService orderService;\n"
                "  private DemoBarcodeService barcodeService;\n"
                "  public Long createCard(DemoCardReqVO reqVO) {\n"
                "    DemoOrderDO ord = orderService.validateOrderConfirmed(reqVO.getOrderId());\n"
                "    if (ObjUtil.notEqual(ord.getProductId(), reqVO.getItemId())) { throw exception(1); }\n"
                "    DemoCardDO card = BeanUtils.toBean(reqVO, DemoCardDO.class);\n"
                "    cardMapper.insert(card);\n"
                "    barcodeService.autoGenerateBarcode(\"CARD\", card.getId());\n"
                "    return card.getId();\n"
                "  }\n"
                "}\n", encoding="utf-8",
            )

            # 2) W0 §6 Item 2 + Item 17: conditional destructive delete with live readback vs unsafe unconditional delete
            (mod / "controller/admin/rule/DemoRuleController.java").write_text(
                "@RequestMapping(\"/demo/rule\")\npublic class DemoRuleController {\n"
                "  private DemoRuleService ruleService;\n"
                "  @PutMapping(\"/update\") public CommonResult<Boolean> updateRule(@RequestBody DemoRuleReqVO reqVO) { ruleService.updateRule(reqVO); return success(true); }\n"
                "  @GetMapping(\"/get\") public CommonResult<DemoRuleRespVO> getRule(@RequestParam(\"id\") Long id) { return null; }\n"
                "}\n", encoding="utf-8",
            )
            (mod / "controller/admin/ruleline/DemoRuleLineController.java").write_text(
                "@RequestMapping(\"/demo/rule-line\")\npublic class DemoRuleLineController {\n"
                "  private DemoRuleLineService ruleLineService;\n"
                "  @GetMapping(\"/list-by-rule-id\") public CommonResult<List<DemoRuleLineRespVO>> list(@RequestParam(\"ruleId\") Long ruleId) { return success(ruleLineService.getList(ruleId)); }\n"
                "  @GetMapping(\"/page\") public CommonResult<Object> page() { return success(ruleLineService.getPage()); }\n"
                "}\n", encoding="utf-8",
            )
            (mod / "service/rule/DemoRuleServiceImpl.java").write_text(
                "public class DemoRuleServiceImpl implements DemoRuleService {\n"
                "  private DemoRuleMapper ruleMapper;\n"
                "  private DemoRuleLineService ruleLineService;\n"
                "  @Transactional(rollbackFor = Exception.class)\n"
                "  public void updateRule(DemoRuleReqVO reqVO) {\n"
                "    DemoRuleDO exist = ruleMapper.selectById(reqVO.getId());\n"
                "    boolean schemeChanged = ObjUtil.notEqual(exist.getSchemeId(), reqVO.getSchemeId());\n"
                "    if (schemeChanged) {\n"
                "      ruleLineService.generateLines(reqVO.getId(), false);\n"
                "    }\n"
                "    ruleMapper.updateById(BeanUtils.toBean(reqVO, DemoRuleDO.class));\n"
                "  }\n"
                "}\n", encoding="utf-8",
            )
            (mod / "service/ruleline/DemoRuleLineServiceImpl.java").write_text(
                "public class DemoRuleLineServiceImpl implements DemoRuleLineService {\n"
                "  private DemoRuleLineMapper ruleLineMapper;\n"
                "  public void generateLines(Long ruleId, boolean isCreate) {\n"
                "    if (!isCreate) { ruleLineMapper.deleteByRuleId(ruleId); }\n"
                "    ruleLineMapper.insertBatch(null);\n"
                "  }\n"
                "}\n", encoding="utf-8",
            )

            # Unsafe unconditional delete WITHOUT readback endpoint -> must fail closed at compile time
            (mod / "controller/admin/wh/DemoBadUpdController.java").write_text(
                "@RequestMapping(\"/demo/bad-upd\")\npublic class DemoBadUpdController {\n"
                "  private DemoBadUpdService badUpdService;\n"
                "  @PutMapping(\"/update\") public CommonResult<Boolean> updateBad(@RequestBody DemoRuleReqVO reqVO) { badUpdService.updateBad(reqVO); return success(true); }\n"
                "  @GetMapping(\"/get\") public CommonResult<DemoRuleRespVO> getBad(@RequestParam(\"id\") Long id) { return null; }\n"
                "}\n", encoding="utf-8",
            )
            (mod / "service/wh/DemoBadUpdServiceImpl.java").write_text(
                "public class DemoBadUpdServiceImpl implements DemoBadUpdService {\n"
                "  private DemoBadUpdMapper badUpdMapper;\n"
                "  private DemoUnknownLineMapper unknownLineMapper;\n"
                "  public void updateBad(DemoRuleReqVO reqVO) {\n"
                "    unknownLineMapper.deleteByRuleId(reqVO.getId());\n"
                "    badUpdMapper.updateById(BeanUtils.toBean(reqVO, DemoRuleDO.class));\n"
                "  }\n"
                "}\n", encoding="utf-8",
            )

            # 3) w0-05: get-property companion readback & crm/receivable style payload:planId + expectField
            (mod / "controller/admin/tpl/DemoTplController.java").write_text(
                "@RequestMapping(\"/demo/tpl\")\npublic class DemoTplController {\n"
                "  private DemoTplService tplService;\n"
                "  @PostMapping(\"/create\") public CommonResult<Long> createTpl(@RequestBody DemoTplReqVO reqVO) { return success(tplService.createTpl(reqVO)); }\n"
                "  @GetMapping(\"/get\") public CommonResult<DemoTplRespVO> getTpl(@RequestParam(\"id\") Long id) { return null; }\n"
                "  @GetMapping(\"/get-property\") public CommonResult<DemoTplPropRespVO> getProperty(@RequestParam(\"id\") Long id) { return null; }\n"
                "}\n", encoding="utf-8",
            )
            (mod / "controller/admin/tplpage/DemoTplPageController.java").write_text(
                "@RequestMapping(\"/demo/tpl-page\")\npublic class DemoTplPageController {\n"
                "  private DemoTplPageService tplPageService;\n"
                "  @GetMapping(\"/list\") public CommonResult<List<Object>> list(@RequestParam(\"ids\") String ids) { return success(tplPageService.getList(ids)); }\n"
                "}\n", encoding="utf-8",
            )
            (mod / "service/tpl/DemoTplServiceImpl.java").write_text(
                "public class DemoTplServiceImpl implements DemoTplService {\n"
                "  private DemoTplMapper tplMapper;\n"
                "  private DemoTplPageService tplPageService;\n"
                "  public Long createTpl(DemoTplReqVO reqVO) {\n"
                "    DemoTplDO t = BeanUtils.toBean(reqVO, DemoTplDO.class);\n"
                "    tplMapper.insert(t);\n"
                "    tplPageService.createDefaultPage(t.getId());\n"
                "    return t.getId();\n"
                "  }\n"
                "}\n", encoding="utf-8",
            )
            (mod / "service/tplpage/DemoTplPageServiceImpl.java").write_text(
                "public class DemoTplPageServiceImpl implements DemoTplPageService {\n"
                "  private DemoTplPageMapper tplPageMapper;\n"
                "  public void createDefaultPage(Long tplId) { tplPageMapper.insert(null); }\n"
                "}\n", encoding="utf-8",
            )
            (mod / "controller/admin/pay/DemoPayController.java").write_text(
                "@RequestMapping(\"/demo/pay\")\npublic class DemoPayController {\n"
                "  private DemoPayService payService;\n"
                "  @PostMapping(\"/create\") public CommonResult<Long> createPay(@RequestBody DemoPayReqVO reqVO) { return success(payService.createPay(reqVO)); }\n"
                "  @GetMapping(\"/get\") public CommonResult<DemoPayRespVO> getPay(@RequestParam(\"id\") Long id) { return null; }\n"
                "}\n", encoding="utf-8",
            )
            (mod / "controller/admin/plan/DemoPlanController.java").write_text(
                "@RequestMapping(\"/demo/plan\")\npublic class DemoPlanController {\n"
                "  private DemoPlanService planService;\n"
                "  @GetMapping(\"/get\") public CommonResult<DemoPlanRespVO> getPlan(@RequestParam(\"id\") Long id) { return success(planService.getPlan(id)); }\n"
                "  @GetMapping(\"/page\") public CommonResult<Object> page() { return success(planService.getPage()); }\n"
                "}\n", encoding="utf-8",
            )
            (mod / "service/pay/DemoPayServiceImpl.java").write_text(
                "public class DemoPayServiceImpl implements DemoPayService {\n"
                "  private DemoPayMapper payMapper;\n"
                "  private DemoPlanService planService;\n"
                "  public Long createPay(DemoPayReqVO reqVO) {\n"
                "    DemoPayDO p = BeanUtils.toBean(reqVO, DemoPayDO.class);\n"
                "    payMapper.insert(p);\n"
                "    if (reqVO.getPlanId() != null) { planService.bindPayId(reqVO.getPlanId(), p.getId()); }\n"
                "    return p.getId();\n"
                "  }\n"
                "}\n", encoding="utf-8",
            )
            (mod / "service/plan/DemoPlanServiceImpl.java").write_text(
                "public class DemoPlanServiceImpl implements DemoPlanService {\n"
                "  private DemoPlanMapper planMapper;\n"
                "  public void bindPayId(Long id, Long payId) {\n"
                "    planMapper.updateById(new DemoPlanDO().setId(id).setPayId(payId));\n"
                "  }\n"
                "}\n", encoding="utf-8",
            )

            # 4) w0-12: derived display fields (`keeperUserId` -> `keeperName`) + boolean `frozen`
            (mod / "controller/admin/seal/DemoSealController.java").write_text(
                "@RequestMapping(\"/demo/seal\")\npublic class DemoSealController {\n"
                "  private DemoSealService sealService;\n"
                "  @PutMapping(\"/update\") public CommonResult<Boolean> updateSeal(@RequestBody DemoSealReqVO reqVO) { sealService.updateSeal(reqVO); return success(true); }\n"
                "  @GetMapping(\"/get\") public CommonResult<DemoSealRespVO> getSeal(@RequestParam(\"id\") Long id) { return null; }\n"
                "}\n", encoding="utf-8",
            )
            (mod / "service/seal/DemoSealServiceImpl.java").write_text(
                "public class DemoSealServiceImpl implements DemoSealService {\n"
                "  private DemoSealMapper sealMapper;\n"
                "  public void updateSeal(DemoSealReqVO reqVO) { sealMapper.updateById(BeanUtils.toBean(reqVO, DemoSealDO.class)); }\n"
                "}\n", encoding="utf-8",
            )

            class S1Client(FakeYudaoClient):
                tenant = "1"

                def __init__(self):
                    super().__init__()
                    self.rule = {"id": 10, "name": "规则A", "schemeId": 1, "remark": "旧"}
                    self.rule_lines = [{"id": 101}, {"id": 102}, {"id": 103}]
                    self.seal = {"id": 20, "name": "公章1", "keeperUserId": 5, "keeperName": "张三", "frozen": True}
                    self.schedule = {"id": 30, "title": "周会", "participantUserIds": [101, 102, 103]}
                    self.plans = {9: {"id": 9, "name": "Plan-9", "payId": None}}
                    self.fail_lines = False

                def get(self, path: str, **params):
                    if path == "demo/rule/get":
                        return dict(self.rule)
                    if path == "demo/rule/page":
                        return {"list": [dict(self.rule)]}
                    if path == "demo/rule-line/list-by-rule-id":
                        if self.fail_lines:
                            from web_operation_agent_yudao.reads.client import YudaoReadRefused
                            raise YudaoReadRefused("denied", code=403)
                        return list(self.rule_lines)
                    if path == "demo/tpl/get":
                        return {"id": 7, "name": "Tpl-1"}
                    if path == "demo/tpl/get-property":
                        return {"id": 7, "name": "Tpl-1", "pages": [{"id": 71, "name": "首页"}, {"id": 72, "name": "我的"}]}
                    if path == "demo/plan/get":
                        return dict(self.plans[int(params["id"])])
                    if path == "demo/plan/page":
                        return {"list": list(self.plans.values())}
                    if path == "demo/seal/get":
                        return dict(self.seal)
                    if path == "demo/seal/page":
                        return {"list": [dict(self.seal)]}
                    if path == "system/user/simple-list":
                        return [{"id": 5, "nickname": "张三"}, {"id": 6, "nickname": "李四"}]
                    if path == "system/user/page":
                        return {"list": [{"id": 5, "nickname": "张三"}, {"id": 6, "nickname": "李四"}]}
                    return super().get(path, **params)

                def write_once(self, method: str, path: str, *, body=None, **_kw):
                    if path == "demo/rule/update":
                        old_s = self.rule["schemeId"]
                        self.rule.update(body or {})
                        if self.rule["schemeId"] != old_s:
                            self.rule_lines = [{"id": 201}]
                        return True
                    if path == "demo/tpl/create":
                        return 7
                    if path == "demo/pay/create":
                        if (body or {}).get("planId"):
                            self.plans[int(body["planId"])]["payId"] = 55
                        return 55
                    if path == "demo/seal/update":
                        self.seal.update(body or {})
                        if self.seal.get("keeperUserId") == 6:
                            self.seal["keeperName"] = "李四"
                        return True
                    return super().write_once(method, path, body=body)

            docs = {
                "paths": {
                    "/admin-api/demo/cfg/create": {"post": {"requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/CfgReq"}}}}, "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}}}},
                    "/admin-api/demo/cfg/get": {"get": {"parameters": [], "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CfgResp"}}}}}}},
                    "/admin-api/demo/card/create": {"post": {"requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/CardReq"}}}}, "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}}}},
                    "/admin-api/demo/card/get": {"get": {"parameters": [{"name": "id", "in": "query", "required": True}], "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CardResp"}}}}}}},
                    "/admin-api/demo/rule/update": {"put": {"requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/RuleReq"}}}}, "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultBoolean"}}}}}}},
                    "/admin-api/demo/rule/get": {"get": {"parameters": [{"name": "id", "in": "query", "required": True}], "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/RuleResp"}}}}}}},
                    "/admin-api/demo/rule/page": {"get": {"parameters": [], "responses": {"200": {"content": {"*/*": {"schema": {"type": "object"}}}}}}},
                    "/admin-api/demo/rule-line/list-by-rule-id": {"get": {"parameters": [{"name": "ruleId", "in": "query", "required": True}], "responses": {"200": {"content": {"*/*": {"schema": {"type": "array"}}}}}}},
                    "/admin-api/demo/rule-line/page": {"get": {"parameters": [], "responses": {"200": {"content": {"*/*": {"schema": {"type": "object"}}}}}}},
                    "/admin-api/demo/bad-upd/update": {"put": {"requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/RuleReq"}}}}, "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultBoolean"}}}}}}},
                    "/admin-api/demo/bad-upd/get": {"get": {"parameters": [{"name": "id", "in": "query", "required": True}], "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/RuleResp"}}}}}}},
                    "/admin-api/demo/tpl/create": {"post": {"requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/TplReq"}}}}, "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}}}},
                    "/admin-api/demo/tpl/get": {"get": {"parameters": [{"name": "id", "in": "query", "required": True}], "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/TplResp"}}}}}}},
                    "/admin-api/demo/tpl/get-property": {"get": {"parameters": [{"name": "id", "in": "query", "required": True}], "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/TplPropResp"}}}}}}},
                    "/admin-api/demo/tpl-page/list": {"get": {"parameters": [{"name": "ids", "in": "query", "required": True}], "responses": {"200": {"content": {"*/*": {"schema": {"type": "array"}}}}}}},
                    "/admin-api/demo/pay/create": {"post": {"requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/PayReq"}}}}, "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}}}},
                    "/admin-api/demo/pay/get": {"get": {"parameters": [{"name": "id", "in": "query", "required": True}], "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/PayResp"}}}}}}},
                    "/admin-api/demo/plan/get": {"get": {"parameters": [{"name": "id", "in": "query", "required": True}], "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/PlanResp"}}}}}}},
                    "/admin-api/demo/plan/page": {"get": {"parameters": [], "responses": {"200": {"content": {"*/*": {"schema": {"type": "object"}}}}}}},
                    "/admin-api/demo/seal/update": {"put": {"requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/SealReq"}}}}, "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultBoolean"}}}}}}},
                    "/admin-api/demo/seal/get": {"get": {"parameters": [{"name": "id", "in": "query", "required": True}], "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/SealResp"}}}}}}},
                    "/admin-api/demo/seal/page": {"get": {"parameters": [], "responses": {"200": {"content": {"*/*": {"schema": {"type": "object"}}}}}}},
                    "/admin-api/system/user/simple-list": {"get": {"parameters": [], "responses": {"200": {"content": {"*/*": {"schema": {"type": "array"}}}}}}},
                    "/admin-api/system/user/page": {"get": {"parameters": [], "responses": {"200": {"content": {"*/*": {"schema": {"type": "object"}}}}}}},
                },
                "components": {
                    "schemas": {
                        "CommonResultLong": {"type": "object"},
                        "CommonResultBoolean": {"type": "object"},
                        "CfgReq": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}},
                        "CfgResp": {"type": "object", "properties": {"data": {"type": "object", "properties": {"id": {"type": "integer"}, "name": {"type": "string"}}}}},
                        "CardReq": {"type": "object", "required": ["orderId", "itemId"], "properties": {"orderId": {"type": "integer"}, "itemId": {"type": "integer"}}},
                        "CardResp": {"type": "object", "properties": {"data": {"type": "object", "properties": {"id": {"type": "integer"}, "orderId": {"type": "integer"}, "itemId": {"type": "integer"}}}}},
                        "RuleReq": {"type": "object", "required": ["id", "name"], "properties": {"id": {"type": "integer"}, "name": {"type": "string"}, "schemeId": {"type": "integer"}, "remark": {"type": "string"}}},
                        "RuleResp": {"type": "object", "properties": {"data": {"type": "object", "properties": {"id": {"type": "integer"}, "name": {"type": "string"}, "schemeId": {"type": "integer"}, "remark": {"type": "string"}}}}},
                        "TplReq": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}},
                        "TplResp": {"type": "object", "properties": {"data": {"type": "object", "properties": {"id": {"type": "integer"}, "name": {"type": "string"}}}}},
                        "TplPropResp": {"type": "object", "properties": {"data": {"type": "object", "properties": {"id": {"type": "integer"}, "pages": {"type": "array", "items": {"type": "object"}}}}}},
                        "PayReq": {"type": "object", "required": ["code"], "properties": {"code": {"type": "string"}, "planId": {"type": "integer"}}},
                        "PayResp": {"type": "object", "properties": {"data": {"type": "object", "properties": {"id": {"type": "integer"}, "code": {"type": "string"}, "planId": {"type": "integer"}}}}},
                        "PlanResp": {"type": "object", "properties": {"data": {"type": "object", "properties": {"id": {"type": "integer"}, "name": {"type": "string"}, "payId": {"type": "integer"}}}}},
                        "SealReq": {"type": "object", "required": ["id", "name"], "properties": {"id": {"type": "integer"}, "name": {"type": "string"}, "keeperUserId": {"type": "integer"}, "frozen": {"type": "boolean", "description": "是否冻结"}}},
                        "SealResp": {"type": "object", "properties": {"data": {"type": "object", "properties": {"id": {"type": "integer"}, "name": {"type": "string"}, "keeperUserId": {"type": "integer"}, "keeperName": {"type": "string"}, "frozen": {"type": "boolean"}}}}},
                    }
                },
            }

            client = S1Client()
            port = YudaoConventionWritePort(client, FakeGoalData(docs, source=YudaoSource(troot, release="v-s1-step2")))
            self.assertEqual(write_capabilities_of(port), WRITE_PORT_OPTIONAL)

            creates = port.create_specs()
            updates = port.update_specs()
            self.assertNotIn("demo/cfg", creates)
            self.assertNotIn("demo/card", creates)
            self.assertNotIn("demo/bad-upd", updates)
            self.assertIn("demo/rule", updates)
            self.assertIn("demo/tpl", creates)
            self.assertIn("demo/pay", creates)
            self.assertIn("demo/seal", updates)

            # w0-05 get-property readback -> verified=True
            tpl_se = creates["demo/tpl"].side_effects[0]
            self.assertEqual((tpl_se.readback_entity, tpl_se.readback_query_field), ("get-property:demo/tpl", "id"))
            tpl_rb = port.verify_companion_readback(creates["demo/tpl"], 7, {}, before_record=None, after_record={"id": 7}, side_effects=[tpl_se.to_dict()])
            self.assertTrue(tpl_rb[0]["verified"])

            # crm/receivable style payload:planId + readback_expect_field="payId"
            pay_se = creates["demo/pay"].side_effects[0]
            self.assertEqual((pay_se.readback_entity, pay_se.readback_query_field, pay_se.readback_expect_field), ("demo/plan", "payload:planId", "payId"))
            pay_handler = CompiledCreateHandler(port)
            pay_mut = pay_handler.prepare({"entity": "demo/pay", "fields": [{"field": "code", "value": "P-1"}, {"field": "planId", "value": "Plan-9（ID=9）"}]}, context=_ctx())
            client.write_once("POST", "demo/pay/create", body=pay_mut.args["payload"])
            pay_rb = port.verify_companion_readback(creates["demo/pay"], 55, pay_mut.args["payload"], before_record=None, after_record={"id": 55}, side_effects=pay_mut.args["side_effects"])
            self.assertTrue(pay_rb[0]["verified"])

            # Item 17: conditional destructive delete admits non-triggering update, discloses count on triggering update, refuses drift, fails closed on unreadable count
            rule_handler = ConventionUpdateHandler(port)
            mut_safe = rule_handler.prepare({"entity": "demo/rule", "target": "规则A（ID=10）", "changes": [{"field": "remark", "value": "只改备注"}]}, context=_ctx())
            self.assertIn("本次未触发", "\n".join(d.text for d in mut_safe.details if d.kind == "side_effect"))
            res_safe = rule_handler.dispatch(mut_safe.args, context=_ctx())
            self.assertTrue(res_safe.content["verified"])

            mut_trig = rule_handler.prepare({"entity": "demo/rule", "target": "规则A（ID=10）", "changes": [{"field": "schemeId", "value": 2}]}, context=_ctx())
            self.assertIn("受影响 3 条记录", "\n".join(d.text for d in mut_trig.details if d.kind == "side_effect"))
            res_trig = rule_handler.dispatch(mut_trig.args, context=_ctx())
            self.assertTrue(res_trig.content["verified"])

            mut_drift = rule_handler.prepare({"entity": "demo/rule", "target": "规则A（ID=10）", "changes": [{"field": "schemeId", "value": 3}]}, context=_ctx())
            client.rule_lines = [{"id": 999}, {"id": 1000}]
            with self.assertRaises(BusinessMutationRefusedError):
                rule_handler.dispatch(mut_drift.args, context=_ctx())

            client.fail_lines = True
            with self.assertRaises(BusinessToolRequestError):
                rule_handler.prepare({"entity": "demo/rule", "target": "规则A（ID=10）", "changes": [{"field": "schemeId", "value": 4}]}, context=_ctx())

            # Flat on_remove diff counting (oa/schedule style participantUserIds on before_record)
            from types import SimpleNamespace
            sched_spec = SimpleNamespace(resolve_field=lambda k: SimpleNamespace(name=k, readback_projection=()))
            self.assertEqual(
                port.count_companion_rows(
                    sched_spec,
                    30,
                    {"participantUserIds": [101]},
                    before_record={"id": 30, "participantUserIds": [101, 102, 103]},
                    side_effect={"trigger_mode": "on_remove", "trigger_fields": ["participantUserIds"]},
                ),
                2,
            )

            # w0-12 + W0 §6 Item 9: derived_display_fields on keeperUserId + negated boolean encoding
            seal_spec = updates["demo/seal"]
            self.assertEqual(seal_spec.resolve_field("keeperUserId").derived_display_fields, ("keeperName",))
            seal_handler = ConventionUpdateHandler(port)
            mut_seal = seal_handler.prepare(
                {"entity": "demo/seal", "target": "公章1（ID=20）", "changes": [{"field": "keeperUserId", "value": "李四（ID=6）"}, {"field": "frozen", "value": "不冻结"}]},
                context=_ctx(),
            )
            self.assertEqual(mut_seal.args["payload"]["frozen"], False)
            seal_prop = BusinessMutationProposal(
                tool="update_record",
                args=mut_seal.args,
                effect=mut_seal.effect,
                targets=mut_seal.targets,
                nonce="f" * 32,
                details=mut_seal.details,
                effect_projection=mut_seal.effect_projection,
            )
            seal_handler.validate_provenance(
                mut_seal.args,
                ("把公章改为不冻结，保管人换成 李四（ID=6）",),
                seal_prop,
            )
            res_seal = seal_handler.dispatch(mut_seal.args, context=_ctx())
            self.assertTrue(res_seal.content["verified"])
            self.assertEqual(client.seal["keeperName"], "李四")

    def test_s1_ids_derived_display_scoped_recheck_and_page_fallback(self):
        from web_operation_agent.business.convention_writes import ConventionUpdateHandler
        from web_operation_agent.business.errors import BusinessMutationIndeterminate
        from web_operation_agent_yudao.reads.client import YudaoReadRefused

        with tempfile.TemporaryDirectory() as tmp:
            troot = Path(tmp)
            _write_synthetic_release(troot)
            mod = troot / "server/yudao-module-demo/src/main/java/cn/iocoder/yudao/module/demo"
            for sub in ("controller/admin/sched", "service/sched"):
                (mod / sub).mkdir(parents=True, exist_ok=True)
            (mod / "controller/admin/sched/DemoSchedController.java").write_text(
                "@RequestMapping(\"/demo/sched\")\npublic class DemoSchedController {\n"
                "  private DemoSchedService schedService;\n"
                "  @PutMapping(\"/update\") public CommonResult<Boolean> updateSched(@RequestBody DemoSchedReqVO reqVO) { schedService.updateSched(reqVO); return success(true); }\n"
                "  @GetMapping(\"/get\") public CommonResult<DemoSchedRespVO> getSched(@RequestParam(\"id\") Long id) { return null; }\n"
                "}\n",
                encoding="utf-8",
            )
            (mod / "service/sched/DemoSchedServiceImpl.java").write_text(
                "public class DemoSchedServiceImpl implements DemoSchedService {\n"
                "  private DemoSchedMapper schedMapper;\n"
                "  private DemoSchedLineMapper lineMapper;\n"
                "  public void updateSched(DemoSchedReqVO reqVO) {\n"
                "    schedMapper.updateById(BeanUtils.toBean(reqVO, DemoSchedDO.class));\n"
                "    lineMapper.insertBatch(null);\n"
                "  }\n"
                "}\n",
                encoding="utf-8",
            )

            class IdsClient(FakeYudaoClient):
                tenant = "1"

                def __init__(self):
                    super().__init__()
                    self.sched = {
                        "id": 30,
                        "title": "周会",
                        "description": "原日程",
                        "participantUserIds": [5],
                        "participantUserNames": ["张三"],
                        "participants": [{"userId": 5, "userName": "张三"}],
                    }
                    self.drift_on_desc = False
                    self.ctr_page_rows = [{"id": 701, "name": "合同701", "customerId": 10}]
                    self.calls: list[str] = []

                def get(self, path: str, **params):
                    self.calls.append(path)
                    if path == "demo/sched/get":
                        return {k: (list(v) if isinstance(v, list) else v) for k, v in self.sched.items()}
                    if path == "demo/sched/page":
                        return {"list": [dict(self.sched)]}
                    if path == "system/user/simple-list":
                        return [{"id": 5, "nickname": "张三"}, {"id": 6, "nickname": "李四"}]
                    if path == "mes/wm/stocktaking-task/page":
                        return {"list": [{"id": 44, "name": "盘点44"}]}
                    if path == "mes/wm/stocktaking-task-line/simple-list":
                        if "taskId" not in params:
                            raise YudaoReadRefused("taskId不能为空", code=400)
                        return [{"id": 501, "itemName": "铝材"}] if params["taskId"] == 44 else []
                    if path == "demo/ctr/simple-list":
                        if "customerId" not in params:
                            raise YudaoReadRefused("customerId不能为空", code=400)
                        return [{"id": 702, "name": "合同702", "customerId": 11}] if params["customerId"] == 11 else []
                    if path == "demo/ctr/page":
                        return {"list": list(self.ctr_page_rows)}
                    if path == "demo/customer/simple-list":
                        return [{"id": 11, "name": "客户11"}]
                    return super().get(path, **params)

                def write_once(self, method: str, path: str, *, body=None, **_kw):
                    if path == "demo/sched/update":
                        b = dict(body or {})
                        self.sched["description"] = b.get("description", self.sched["description"])
                        if self.drift_on_desc:
                            self.sched["participants"] = [{"userId": 99, "userName": "漂移"}]
                        if b.get("participantUserIds") != self.sched["participantUserIds"]:
                            uids = list(b.get("participantUserIds") or [])
                            self.sched["participantUserIds"] = uids
                            names = {5: "张三", 6: "李四"}
                            self.sched["participantUserNames"] = [names[u] for u in uids]
                            self.sched["participants"] = [{"userId": u, "userName": names[u]} for u in uids]
                        return True
                    return super().write_once(method, path, body=body)

            docs = {
                "paths": {
                    "/admin-api/demo/sched/update": {"put": {"requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/SchedReq"}}}}, "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultBoolean"}}}}}}},
                    "/admin-api/demo/sched/get": {"get": {"parameters": [{"name": "id", "in": "query", "required": True}], "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/SchedResp"}}}}}}},
                    "/admin-api/demo/sched/page": {"get": {"parameters": [], "responses": {"200": {"content": {"*/*": {"schema": {"type": "object"}}}}}}},
                    "/admin-api/system/user/simple-list": {"get": {"parameters": [], "responses": {"200": {"content": {"*/*": {"schema": {"type": "array"}}}}}}},
                    "/admin-api/mes/wm/stocktaking-task/page": {"get": {"parameters": [], "responses": {"200": {"content": {"*/*": {"schema": {"type": "object"}}}}}}},
                    "/admin-api/mes/wm/stocktaking-task-line/simple-list": {"get": {"parameters": [{"name": "taskId", "in": "query", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"content": {"*/*": {"schema": {"type": "array"}}}}}}},
                    "/admin-api/demo/customer/simple-list": {"get": {"parameters": [], "responses": {"200": {"content": {"*/*": {"schema": {"type": "array"}}}}}}},
                    "/admin-api/demo/ctr/simple-list": {"get": {"parameters": [{"name": "customerId", "in": "query", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"content": {"*/*": {"schema": {"type": "array"}}}}}}},
                    "/admin-api/demo/ctr/page": {"get": {"parameters": [{"name": "pageNo", "in": "query", "required": True}, {"name": "pageSize", "in": "query", "required": True}], "responses": {"200": {"content": {"*/*": {"schema": {"type": "object"}}}}}}},
                },
                "components": {
                    "schemas": {
                        "CommonResultBoolean": {"type": "object"},
                        "SchedParticipant": {"type": "object", "properties": {"userId": {"type": "integer"}, "userName": {"type": "string"}}},
                        "SchedReq": {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}, "title": {"type": "string"}, "description": {"type": "string"}, "participantUserIds": {"type": "array", "items": {"type": "integer"}}}},
                        "SchedResp": {
                            "type": "object",
                            "properties": {
                                "data": {
                                    "type": "object",
                                    "properties": {
                                        "id": {"type": "integer"},
                                        "title": {"type": "string"},
                                        "description": {"type": "string"},
                                        "participantUserIds": {"type": "array", "items": {"type": "integer"}},
                                        "participantUserNames": {"type": "array", "items": {"type": "string"}},
                                        "participants": {"type": "array", "items": {"$ref": "#/components/schemas/SchedParticipant"}},
                                    },
                                }
                            },
                        },
                    }
                },
            }

            client = IdsClient()
            port = YudaoConventionWritePort(client, FakeGoalData(docs, source=YudaoSource(troot, release="v-s1-ids")))
            sched_spec = port.update_specs()["demo/sched"]
            self.assertEqual(
                sched_spec.resolve_field("participantUserIds").derived_display_fields,
                ("participantUserNames", "participants"),
            )

            handler = ConventionUpdateHandler(port)
            # Updating participantUserIds allows derived participantUserNames + participants changes
            mut_ids = handler.prepare(
                {"entity": "demo/sched", "target": "周会（ID=30）", "changes": [{"field": "participantUserIds", "value": ["张三（ID=5）", "李四（ID=6）"]}]},
                context=_ctx(),
            )
            res_ids = handler.dispatch(mut_ids.args, context=_ctx())
            self.assertTrue(res_ids.content["verified"])

            # Updating description still catches unexpected drift in participants
            client.drift_on_desc = True
            mut_desc = handler.prepare(
                {"entity": "demo/sched", "target": "周会（ID=30）", "changes": [{"field": "description", "value": "改描述"}]},
                context=_ctx(),
            )
            with self.assertRaises(BusinessMutationIndeterminate):
                handler.dispatch(mut_desc.args, context=_ctx())

            # Required taskId in simple-list is carried into binding query_params for recheck_bindings
            line_id, _, line_bind = port._resolve_single_ref("mes/wm/stocktaking-task-line", "铝材（ID=501）")
            self.assertEqual(line_id, 501)
            self.assertEqual(line_bind["query_params"].get("taskId"), 44)
            port.recheck_bindings([line_bind], actor_id="1")

            # Scoped simple-list prefers /page before scanning parents, and falls back when /page is empty
            lp1, _, rows1, qp1 = port._list_ref_rows("demo/ctr")
            self.assertEqual(lp1, "demo/ctr/page")
            self.assertEqual([r["id"] for r in rows1], [701])
            self.assertNotIn("demo/customer/simple-list", client.calls)

            client.ctr_page_rows = []
            lp2, _, rows2, qp2 = port._list_ref_rows("demo/ctr")
            self.assertEqual(lp2, "demo/ctr/simple-list")
            self.assertEqual([r["id"] for r in rows2], [702])
            self.assertEqual(qp2, {})

    def test_s1_f2_precision_helper_forwarding_tiers_required_empty_defaults_and_child_ctx(self):
        from web_operation_agent_yudao.rules.compiled_write_source import CompiledFieldDefault, _infer_f2_reference
        from web_operation_agent_yudao.rules.ui_source import UiSource

        with tempfile.TemporaryDirectory() as tmp:
            troot = Path(tmp)
            _write_synthetic_release(troot)
            mod = troot / "server/yudao-module-demo/src/main/java/cn/iocoder/yudao/module/demo"
            for sub in ("controller/admin/iqc", "controller/admin/ws", "controller/admin/rec", "service/iqc", "service/ws", "service/rec"):
                (mod / sub).mkdir(parents=True, exist_ok=True)
            ui_view = troot / "ui/src/views/demo/rec"
            ui_view.mkdir(parents=True, exist_ok=True)
            (troot / "ui/src/router/modules").mkdir(parents=True, exist_ok=True)
            (troot / "ui/src/router/modules/remaining.ts").write_text("export default []\n", encoding="utf-8")
            (troot / "ui/src/api/demo").mkdir(parents=True, exist_ok=True)
            (troot / "ui/src/api/demo/rec.ts").write_text(
                "export const createRec = (data: any) => {\n"
                "  return request.post({ url: '/demo/rec/create', data })\n"
                "}\n",
                encoding="utf-8",
            )
            (ui_view / "RecForm.vue").write_text(
                "<template><el-form :model='formData'></el-form></template>\n"
                "<script setup lang='ts'>\n"
                "import * as RecApi from '@/api/demo/rec'\n"
                "const formData = ref({ optTags: [], reqTags: [], name: '' })\n"
                "const resetForm = () => { formData.value = { optTags: [], reqTags: [], name: '' } }\n"
                "const submitForm = async () => { await RecApi.createRec(formData.value) }\n"
                "</script>\n",
                encoding="utf-8",
            )

            # IQC: single-arg itemService.validateItemExists(reqVO.getItemId()) vs multi-arg child lookup
            (mod / "controller/admin/iqc/DemoIqcController.java").write_text(
                "@RequestMapping(\"/demo/iqc\")\npublic class DemoIqcController {\n"
                "  private DemoIqcService iqcService;\n"
                "  @PostMapping(\"/create\") public CommonResult<Long> createIqc(@RequestBody IqcReqVO reqVO) { return success(iqcService.createIqc(reqVO)); }\n"
                "  @GetMapping(\"/get\") public CommonResult<IqcRespVO> getIqc(@RequestParam(\"id\") Long id) { return null; }\n"
                "}\n",
                encoding="utf-8",
            )
            (mod / "service/iqc/DemoIqcServiceImpl.java").write_text(
                "public class DemoIqcServiceImpl implements DemoIqcService {\n"
                "  private DemoIqcMapper iqcMapper;\n"
                "  private DemoMdItemService itemService;\n"
                "  private DemoTplItemService tplItemService;\n"
                "  public Long createIqc(IqcReqVO reqVO) {\n"
                "    itemService.validateItemExists(reqVO.getItemId());\n"
                "    tplItemService.getRequiredTemplateByItemIdAndType(reqVO.getItemId(), 1);\n"
                "    iqcMapper.insert(BeanUtils.toBean(reqVO, DemoIqcDO.class));\n"
                "    return 1L;\n"
                "  }\n"
                "}\n",
                encoding="utf-8",
            )

            # WS: 1-hop private helper forwarding validateWorkshopExists(reqVO.getWorkshopId()) -> workshopService.getWorkshop(workshopId)
            (mod / "controller/admin/ws/DemoWsController.java").write_text(
                "@RequestMapping(\"/demo/ws\")\npublic class DemoWsController {\n"
                "  private DemoWsService wsService;\n"
                "  @PostMapping(\"/create\") public CommonResult<Long> createWs(@RequestBody WsReqVO reqVO) { return success(wsService.createWs(reqVO)); }\n"
                "  @GetMapping(\"/get\") public CommonResult<WsRespVO> getWs(@RequestParam(\"id\") Long id) { return null; }\n"
                "}\n",
                encoding="utf-8",
            )
            (mod / "service/ws/DemoWsServiceImpl.java").write_text(
                "public class DemoWsServiceImpl implements DemoWsService {\n"
                "  private DemoWsMapper wsMapper;\n"
                "  private DemoMdWorkshopService workshopService;\n"
                "  public Long createWs(WsReqVO reqVO) {\n"
                "    validateWsSave(reqVO);\n"
                "    wsMapper.insert(BeanUtils.toBean(reqVO, DemoWsDO.class));\n"
                "    return 1L;\n"
                "  }\n"
                "  private void validateWsSave(WsReqVO reqVO) { validateWorkshopExists(reqVO.getWorkshopId()); }\n"
                "  private void validateWorkshopExists(Long workshopId) { workshopService.getWorkshop(workshopId); }\n"
                "}\n",
                encoding="utf-8",
            )

            # REC: required @NotEmpty [] default stripped while optional [] preserved; child collection gets f2_ctx
            (mod / "controller/admin/rec/vo").mkdir(parents=True, exist_ok=True)
            (mod / "controller/admin/rec/vo/RecReqVO.java").write_text(
                "public class RecReqVO {\n"
                "  @NotBlank private String name;\n"
                "  @NotEmpty private List<Integer> reqTags;\n"
                "  private List<Integer> optTags;\n"
                "  private List<RecItem> items;\n"
                "}\n",
                encoding="utf-8",
            )
            (mod / "controller/admin/rec/DemoRecController.java").write_text(
                "@RequestMapping(\"/demo/rec\")\npublic class DemoRecController {\n"
                "  private DemoRecService recService;\n"
                "  @PostMapping(\"/create\") public CommonResult<Long> createRec(@RequestBody RecReqVO reqVO) { return success(recService.createRec(reqVO)); }\n"
                "  @GetMapping(\"/get\") public CommonResult<RecRespVO> getRec(@RequestParam(\"id\") Long id) { return null; }\n"
                "}\n",
                encoding="utf-8",
            )
            (mod / "service/rec/DemoRecServiceImpl.java").write_text(
                "public class DemoRecServiceImpl implements DemoRecService {\n"
                "  private DemoRecMapper recMapper;\n"
                "  private DemoRecLineMapper recLineMapper;\n"
                "  public Long createRec(RecReqVO reqVO) {\n"
                "    recMapper.insert(BeanUtils.toBean(reqVO, DemoRecDO.class));\n"
                "    recLineMapper.insertBatch(null);\n"
                "    return 1L;\n"
                "  }\n"
                "}\n",
                encoding="utf-8",
            )

            src = YudaoSource(troot, release="v-s1-f2p")
            ui = UiSource.read(troot / "ui")
            docs = {
                "paths": {
                    "/admin-api/demo/iqc/create": {"post": {"requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/IqcReqVO"}}}}, "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}}}},
                    "/admin-api/demo/iqc/get": {"get": {"parameters": [{"name": "id", "in": "query", "required": True}], "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/IqcRespVO"}}}}}}},
                    "/admin-api/demo/ws/create": {"post": {"requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/WsReqVO"}}}}, "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}}}},
                    "/admin-api/demo/ws/get": {"get": {"parameters": [{"name": "id", "in": "query", "required": True}], "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/WsRespVO"}}}}}}},
                    "/admin-api/demo/rec/create": {"post": {"requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/RecReqVO"}}}}, "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}}}},
                    "/admin-api/demo/rec/get": {"get": {"parameters": [{"name": "id", "in": "query", "required": True}], "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/RecRespVO"}}}}}}},
                    "/admin-api/demo/md/item/page": {"get": {"parameters": [], "responses": {"200": {"content": {"*/*": {"schema": {"type": "object"}}}}}}},
                    "/admin-api/demo/md-workshop/page": {"get": {"parameters": [], "responses": {"200": {"content": {"*/*": {"schema": {"type": "object"}}}}}}},
                    "/admin-api/demo/wm/warehouse-area/page": {"get": {"parameters": [], "responses": {"200": {"content": {"*/*": {"schema": {"type": "object"}}}}}}},
                    "/admin-api/demo/wm/stocktaking-task/page": {"get": {"parameters": [], "responses": {"200": {"content": {"*/*": {"schema": {"type": "object"}}}}}}},
                },
                "components": {
                    "schemas": {
                        "CommonResultLong": {"type": "object"},
                        "IqcReqVO": {"type": "object", "required": ["itemId"], "properties": {"itemId": {"type": "integer"}}},
                        "IqcRespVO": {"type": "object", "properties": {"data": {"type": "object", "properties": {"id": {"type": "integer"}, "itemId": {"type": "integer"}}}}},
                        "WsReqVO": {"type": "object", "required": ["workshopId", "fromAreaId"], "properties": {"workshopId": {"type": "integer"}, "fromAreaId": {"type": "integer"}}},
                        "WsRespVO": {"type": "object", "properties": {"data": {"type": "object", "properties": {"id": {"type": "integer"}, "workshopId": {"type": "integer"}, "fromAreaId": {"type": "integer"}}}}},
                        "RecItem": {"type": "object", "required": ["itemId"], "properties": {"itemId": {"type": "integer"}}},
                        "RecReqVO": {"type": "object", "required": ["name", "reqTags"], "properties": {"name": {"type": "string"}, "reqTags": {"type": "array", "items": {"type": "integer"}}, "optTags": {"type": "array", "items": {"type": "integer"}}, "items": {"type": "array", "items": {"$ref": "#/components/schemas/RecItem"}}}},
                        "RecRespVO": {"type": "object", "properties": {"data": {"type": "object", "properties": {"id": {"type": "integer"}, "name": {"type": "string"}, "reqTags": {"type": "array", "items": {"type": "integer"}}, "optTags": {"type": "array", "items": {"type": "integer"}}, "items": {"type": "array", "items": {"$ref": "#/components/schemas/RecItem"}}}}}},
                    }
                },
            }
            gd = FakeGoalData(docs, source=src)
            gd._ui = ui
            port = YudaoConventionWritePort(FakeYudaoClient(), gd)
            creates = port.create_specs()

            # Single-arg Source A + 1-hop helper forwarding + directional Tier 4 + child collection f2_ctx
            self.assertEqual(creates["demo/iqc"].resolve_field("itemId").reference_entity, "demo/md/item")
            self.assertEqual(creates["demo/ws"].resolve_field("workshopId").reference_entity, "demo/md-workshop")
            self.assertEqual(creates["demo/ws"].resolve_field("fromAreaId").reference_entity, "demo/wm/warehouse-area")
            self.assertEqual(creates["demo/rec"].collections[0].resolve_field("itemId").reference_entity, "demo/md/item")

            # Parent-prefix Tier 2 match
            self.assertEqual(
                _infer_f2_reference(
                    "demo/wm/stocktaking-task-result", "taskId", "integer", None,
                    source=src, svc_by_iface={}, parsed_svc_cache={},
                    listable_entities={"demo/wm/stocktaking-task", "demo/pro/task"},
                    unique_svc_entity={}, ui_field_refs={},
                ),
                "demo/wm/stocktaking-task",
            )

            # Empty array literal default rejected for required field, preserved for optional field
            rec_defs = {d.field: d for d in creates["demo/rec"].defaults}
            self.assertNotIn("reqTags", rec_defs)
            self.assertEqual(rec_defs["optTags"], CompiledFieldDefault("optTags", "literal", [], "[]（空列表）"))

    def test_s1_step2_qualified_f2_validate2_polymorphic_and_tier25_subprefix(self):
        from web_operation_agent_yudao.rules.compiled_write_source import _infer_f2_reference

        with tempfile.TemporaryDirectory() as tmp:
            troot = Path(tmp)
            (troot / "java/demo").mkdir(parents=True)

            (troot / "java/demo/DemoMdVendorServiceImpl.java").write_text(
                "public class DemoMdVendorServiceImpl implements DemoMdVendorService {}\n", encoding="utf-8"
            )
            (troot / "java/demo/DemoMdVendorController.java").write_text(
                '@RequestMapping("/demo/md-vendor")\npublic class DemoMdVendorController { private DemoMdVendorService s; }\n',
                encoding="utf-8",
            )
            (troot / "java/demo/DemoDvCheckPlanServiceImpl.java").write_text(
                "public class DemoDvCheckPlanServiceImpl implements DemoDvCheckPlanService {}\n", encoding="utf-8"
            )
            (troot / "java/demo/DemoDvCheckPlanController.java").write_text(
                '@RequestMapping("/demo/dv/check-plan")\npublic class DemoDvCheckPlanController { private DemoDvCheckPlanService s; }\n',
                encoding="utf-8",
            )
            (troot / "java/demo/DemoCalPlanServiceImpl.java").write_text(
                "public class DemoCalPlanServiceImpl implements DemoCalPlanService {}\n", encoding="utf-8"
            )
            (troot / "java/demo/DemoCalPlanController.java").write_text(
                '@RequestMapping("/demo/cal/plan")\npublic class DemoCalPlanController { private DemoCalPlanService s; }\n',
                encoding="utf-8",
            )
            (troot / "java/demo/DemoArrivalServiceImpl.java").write_text(
                """
                public class DemoArrivalServiceImpl implements DemoArrivalService {
                    private cn.iocoder.demo.DemoMdVendorService vendorService;
                    public Long createArrival(ArrivalReqVO reqVO) {
                        vendorService.validateVendorExists(reqVO.getVendorId());
                        return 1L;
                    }
                }
                """,
                encoding="utf-8",
            )
            (troot / "java/demo/DemoArrivalController.java").write_text(
                """
                @RequestMapping("/demo/wm/arrival-notice")
                public class DemoArrivalController {
                    private DemoArrivalService arrivalService;
                    @PostMapping("/create")
                    public CommonResult<Long> create(@RequestBody ArrivalReqVO reqVO) {
                        return success(arrivalService.createArrival(reqVO));
                    }
                }
                """,
                encoding="utf-8",
            )
            (troot / "java/demo/DemoDvCheckRecordServiceImpl.java").write_text(
                """
                public class DemoDvCheckRecordServiceImpl implements DemoDvCheckRecordService {
                    private DemoDvCheckPlanService checkPlanService;
                    public Long createCheckRecord(CheckRecordReqVO reqVO) {
                        checkPlanService.validateCheckPlanExistsAndType(reqVO.getPlanId(), 1);
                        return 1L;
                    }
                }
                """,
                encoding="utf-8",
            )
            (troot / "java/demo/DemoDvCheckRecordController.java").write_text(
                """
                @RequestMapping("/demo/dv/check-record")
                public class DemoDvCheckRecordController {
                    private DemoDvCheckRecordService checkRecordService;
                    @PostMapping("/create")
                    public CommonResult<Long> create(@RequestBody CheckRecordReqVO reqVO) {
                        return success(checkRecordService.createCheckRecord(reqVO));
                    }
                }
                """,
                encoding="utf-8",
            )
            (troot / "java/demo/DemoQcOqcServiceImpl.java").write_text(
                "public class DemoQcOqcServiceImpl implements DemoQcOqcService { public Long createOqc(OqcReqVO reqVO) { return 1L; } }\n",
                encoding="utf-8",
            )
            (troot / "java/demo/DemoQcOqcController.java").write_text(
                """
                @RequestMapping("/demo/qc/oqc")
                public class DemoQcOqcController {
                    private DemoQcOqcService oqcService;
                    @PostMapping("/create")
                    public CommonResult<Long> create(@RequestBody OqcReqVO reqVO) {
                        return success(oqcService.createOqc(reqVO));
                    }
                }
                """,
                encoding="utf-8",
            )

            src = YudaoSource(troot, release="v-s1-step2-fixes")
            docs = {
                "paths": {
                    "/admin-api/demo/md-vendor/page": {"get": {"parameters": [], "responses": {"200": {"content": {"*/*": {"schema": {"type": "object"}}}}}}},
                    "/admin-api/demo/dv/check-plan/page": {"get": {"parameters": [], "responses": {"200": {"content": {"*/*": {"schema": {"type": "object"}}}}}}},
                    "/admin-api/demo/cal/plan/page": {"get": {"parameters": [], "responses": {"200": {"content": {"*/*": {"schema": {"type": "object"}}}}}}},
                    "/admin-api/demo/wm/product-sales-line/page": {"get": {"parameters": [], "responses": {"200": {"content": {"*/*": {"schema": {"type": "object"}}}}}}},
                    "/admin-api/demo/wm/arrival-notice/create": {"post": {"requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/ArrivalReqVO"}}}}, "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}}}},
                    "/admin-api/demo/wm/arrival-notice/get": {"get": {"parameters": [{"name": "id", "in": "query", "required": True}], "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/ArrivalRespVO"}}}}}}},
                    "/admin-api/demo/dv/check-record/create": {"post": {"requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/CheckRecordReqVO"}}}}, "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}}}},
                    "/admin-api/demo/dv/check-record/get": {"get": {"parameters": [{"name": "id", "in": "query", "required": True}], "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CheckRecordRespVO"}}}}}}},
                    "/admin-api/demo/qc/oqc/create": {"post": {"requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/OqcReqVO"}}}}, "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultLong"}}}}}}},
                    "/admin-api/demo/qc/oqc/get": {"get": {"parameters": [{"name": "id", "in": "query", "required": True}], "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/OqcRespVO"}}}}}}},
                },
                "components": {
                    "schemas": {
                        "CommonResultLong": {"type": "object"},
                        "ArrivalReqVO": {"type": "object", "required": ["vendorId"], "properties": {"vendorId": {"type": "integer"}}},
                        "ArrivalRespVO": {"type": "object", "properties": {"data": {"type": "object", "properties": {"id": {"type": "integer"}, "vendorId": {"type": "integer"}}}}},
                        "CheckRecordReqVO": {"type": "object", "properties": {"planId": {"type": "integer"}}},
                        "CheckRecordRespVO": {"type": "object", "properties": {"data": {"type": "object", "properties": {"id": {"type": "integer"}, "planId": {"type": "integer"}}}}},
                        "OqcReqVO": {"type": "object", "properties": {"sourceDocId": {"type": "integer"}, "sourceLineId": {"type": "integer"}}},
                        "OqcRespVO": {"type": "object", "properties": {"data": {"type": "object", "properties": {"id": {"type": "integer"}, "sourceDocId": {"type": "integer"}, "sourceLineId": {"type": "integer"}}}}},
                    }
                },
            }
            gd = FakeGoalData(docs, source=src)
            gd._ui = None
            port = YudaoConventionWritePort(FakeYudaoClient(), gd)
            creates = port.create_specs()

            self.assertEqual(creates["demo/wm/arrival-notice"].resolve_field("vendorId").reference_entity, "demo/md-vendor")
            self.assertEqual(creates["demo/dv/check-record"].resolve_field("planId").reference_entity, "demo/dv/check-plan")
            self.assertEqual(creates["demo/qc/oqc"].resolve_field("sourceDocId").reference_entity, "")
            self.assertEqual(creates["demo/qc/oqc"].resolve_field("sourceLineId").reference_entity, "")

            # Pure Tier 2.5 submodule suffix preference without Source A
            self.assertEqual(
                _infer_f2_reference(
                    "demo/dv/check-record", "planId", "integer", None,
                    source=src, svc_by_iface={}, parsed_svc_cache={},
                    listable_entities={"demo/dv/check-plan", "demo/cal/plan"},
                    unique_svc_entity={}, ui_field_refs={},
                ),
                "demo/dv/check-plan",
            )

            from web_operation_agent_yudao.rules.compiled_write_source import _infer_extended_reference
            self.assertEqual(
                _infer_extended_reference("oa/travel-apply", "departureAreaId", "integer", "出发地区编号", set()),
                "system/area",
            )
            self.assertEqual(
                _infer_extended_reference("mes/wm/transfer-line", "fromAreaId", "integer", "移出库位编号", set()),
                "",
            )

    def test_s1_step2_boolean_dict_emp_page_paginated_recheck_and_scoped_relation(self):
        from web_operation_agent.business.compiled_writes import (
            CompiledRelationHandler,
            CompiledRelationSpec,
        )
        from web_operation_agent.business.convention_writes import ConventionFieldSpec, ConventionUpdateSpec

        class Step2Client(FakeYudaoClient):
            def __init__(self):
                super().__init__()
                self.records[("demo/item", 1)] = {"id": 1, "name": "物料1"}
                self.records[("demo/item", 105)] = {"id": 105, "name": "物料105"}
                self.records[("pms/pm/work-item", 20)] = {"id": 20, "name": "工作项A", "projectId": 10, "type": 2, "statusId": 301}
                self.status_list_calls: list[dict] = []
                self.wi_status_rows = {
                    (10, 2): [{"id": 301, "name": "待处理"}, {"id": 302, "name": "已完成"}],
                }
                self.item_get_name = "物料105"

            def get(self, path: str, **params):
                if path == "system/dict-data/simple-list":
                    return [
                        {"dictType": "infra_boolean_string", "label": "是", "value": "true"},
                        {"dictType": "infra_boolean_string", "label": "否", "value": "false"},
                    ]
                if path == "demo/item/page":
                    return {"total": 2, "list": [{"id": 1, "name": "物料1"}]}
                if path == "demo/item/get":
                    if int(params["id"]) == 105:
                        return {"id": 105, "name": self.item_get_name}
                    return None
                if path == "pms/pm/work-item/get":
                    return copy.deepcopy(self.records.get(("pms/pm/work-item", int(params["id"]))))
                if path == "pms/pm/work-item-status/list":
                    self.status_list_calls.append(dict(params))
                    return list(self.wi_status_rows.get((int(params["projectId"]), int(params["type"])), []))
                return super().get(path, **params)

        docs = {
            "paths": {
                "/admin-api/hrm/employee/simple-list": {"get": {"parameters": [{"name": "ids", "in": "query", "required": True}], "responses": {"200": {"description": "ok"}}}},
                "/admin-api/hrm/employee/page": {"get": {"parameters": [], "responses": {"200": {"description": "ok"}}}},
                "/admin-api/demo/emp/simple-list": {"get": {"parameters": [{"name": "ids", "in": "query", "required": True}], "responses": {"200": {"description": "ok"}}}},
                "/admin-api/demo/emp/page": {"get": {"parameters": [], "responses": {"200": {"description": "ok"}}}},
                "/admin-api/demo/item/page": {"get": {"parameters": [], "responses": {"200": {"description": "ok"}}}},
                "/admin-api/demo/item/get": {"get": {"parameters": [{"name": "id", "in": "query", "required": True}], "responses": {"200": {"description": "ok"}}}},
                "/admin-api/pms/pm/work-item-status/list": {
                    "get": {
                        "parameters": [
                            {"name": "projectId", "in": "query", "required": True},
                            {"name": "type", "in": "query", "required": True},
                        ],
                        "responses": {"200": {"description": "ok"}},
                    }
                },
            },
            "components": {"schemas": {}},
        }
        client = Step2Client()
        port = YudaoConventionWritePort(client, FakeGoalData(docs))

        # 1. infra_boolean_string dict boolean roundtrip
        bool_field = ConventionFieldSpec(
            name="padded",
            kind="boolean",
            label="是否补齐",
            required=True,
            enum_type="infra_boolean_string",
        )
        dummy_spec = ConventionUpdateSpec(entity="demo/rule", label="编码规则", fields=(bool_field,))
        api_v, old_disp, new_disp, b_dict = port.encode_field(dummy_spec, bool_field, True, {"padded": False})
        self.assertIs(api_v, True)
        self.assertEqual(old_disp, "否")
        self.assertEqual(new_disp, "是")
        self.assertEqual(b_dict["code"], "true")
        port.recheck_bindings([b_dict], actor_id="9")
        self.assertTrue(port.matches_readback_value(dummy_spec, bool_field, True, api_v))

        api_v2, _, new_disp2, _ = port.encode_field(dummy_spec, bool_field, "否", {})
        self.assertIs(api_v2, False)
        self.assertEqual(new_disp2, "否")

        # 2. _ref_endpoint_for skips simple-list requiring `ids` and picks /page
        self.assertEqual(port._ref_endpoint_for("hrm/employee")[0], "hrm/employee/page")
        self.assertEqual(port._ref_endpoint_for("demo/emp")[0], "demo/emp/page")

        # 3. Paginated /page ref past page 1 falls back to /get in _resolve_single_ref & recheck_bindings
        item_field = ConventionFieldSpec(name="itemId", kind="integer", label="物料", required=True, reference_entity="demo/item")
        rid, rlabel, r_bind = port._resolve_single_ref("demo/item", "物料105（ID=105）")
        self.assertEqual(rid, 105)
        self.assertEqual(rlabel, "物料105")
        self.assertEqual(r_bind.get("read_entity"), "demo/item")
        port.recheck_bindings([r_bind], actor_id="9")
        client.item_get_name = "被改名物料"
        with self.assertRaises(BusinessMutationRefusedError):
            port.recheck_bindings([r_bind], actor_id="9")

        # 4. CompiledRelationHandler.prepare forwards required target record query params (projectId, type)
        rel_spec = CompiledRelationSpec(
            entity="pms/pm/work-item",
            label="工作项",
            relation="status",
            relation_label="工作项状态",
            target_entity="pms/pm/work-item-status",
            action_path="pms/pm/work-item/update-status",
            read_path="pms/pm/work-item/get",
            target_param="id",
            items_param="statusId",
            http_method="PUT",
            single_value=True,
        )
        # Monkeypatch relation_specs & read_relation_ids locally for unit verification
        port.relation_specs = lambda: {"pms/pm/work-item": (rel_spec,)}
        port.read_relation_ids = lambda sp, tid: [301]
        handler = CompiledRelationHandler(port)
        prep = handler.prepare(
            {"entity": "pms/pm/work-item", "target": "工作项A（ID=20）", "relation": "status", "items": ["已完成"]},
            context=_ctx(),
        )
        self.assertEqual(prep.args["after_ids"], [302])
        self.assertTrue(any(c == {"projectId": 10, "type": 2} for c in client.status_list_calls))


if __name__ == "__main__":
    unittest.main()
