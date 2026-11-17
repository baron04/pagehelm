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
from web_operation_agent_yudao.convention_writes import YudaoConventionWritePort
from web_operation_agent_yudao.native_submit import _enrich_erp_line_totals
from web_operation_agent_yudao.slim_loop import _unavailable_already_matches
from web_operation_agent_yudao.source import YudaoSource
from web_operation_agent_yudao.writes import native_tools


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
            from web_operation_agent_yudao.ui_source import UiSource
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

            from web_operation_agent_yudao.ui_source import UiSource
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


if __name__ == "__main__":
    unittest.main()
