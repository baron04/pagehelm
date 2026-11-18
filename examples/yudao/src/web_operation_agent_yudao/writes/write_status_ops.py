"""Reviewed typed status-transition operations for ERP documents and Mall promotions."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
import json

from web_operation_agent.business.effects import EffectProjection, RequestedEffect, canonical
from web_operation_agent.business.errors import (
    BusinessMutationIndeterminate,
    BusinessMutationRefusedError,
    BusinessToolPermissionError,
    BusinessToolReferenceError,
    BusinessToolRequestError,
)
from web_operation_agent.business.tools.contracts import BusinessMutationSpec, BusinessToolResult, ToolOperation
from web_operation_agent.business.tools.registry import ToolDefinition
from web_operation_agent.business.write_pipeline import (
    ProposalDiff,
    change_detail,
    execute_verified_handler_write,
    preserved_detail,
    side_effect_detail,
)
from .write_definitions import object_schema, text

ERP_DOCUMENT_ENTITIES = {
    "采购订单": "erp/purchase-order",
    "采购入库单": "erp/purchase-in",
    "采购退货单": "erp/purchase-return",
    "销售订单": "erp/sale-order",
    "销售出库单": "erp/sale-out",
    "销售退货单": "erp/sale-return",
    "其它入库单": "erp/stock-in",
    "其它出库单": "erp/stock-out",
    "库存调拨单": "erp/stock-move",
    "库存盘点单": "erp/stock-check",
    "收款单": "erp/finance-receipt",
    "付款单": "erp/finance-payment",
}
_DOC_TYPE_ALIASES = {
    "库存调度单": "库存调拨单",
    "库存调度": "库存调拨单",
}


def _canonical_doc_type(raw: str) -> str:
    text_val = str(raw or "").strip()
    return _DOC_TYPE_ALIASES.get(text_val, text_val)
_ERP_SIDE_EFFECTS = {
    "采购订单": "变更订单审批状态，决定后续能否生成关联采购入库或退货单据",
    "销售订单": "变更订单审批状态，决定后续能否生成关联销售出库或退货单据",
    "采购入库单": "按单据明细同步变动对应仓库产品库存及关联采购订单入库数量",
    "采购退货单": "按单据明细同步变动对应仓库产品库存及关联采购订单退货数量",
    "销售出库单": "按单据明细同步变动对应仓库产品库存及关联销售订单出库数量",
    "销售退货单": "按单据明细同步变动对应仓库产品库存及关联销售订单退货数量",
    "其它入库单": "按单据明细同步增加对应仓库产品库存",
    "其它出库单": "按单据明细同步扣减对应仓库产品库存",
    "库存调拨单": "按单据明细同步调增调入仓、调减调出仓的产品库存",
    "库存盘点单": "按盘点盈亏明细同步调整对应仓库产品库存",
    "收款单": "同步更新关联销售出库/退货单据的已核销收款金额",
    "付款单": "同步更新关联采购入库/退货单据的已核销付款金额",
}
_ERP_STATUS_LABELS = {10: "未审核", 20: "已审核"}
_PROMO_STATUS_LABELS = {0: "开启", 1: "关闭"}
_ERP_UNAPPROVE_BLOCKERS: dict[str, tuple[tuple[str, str], ...]] = {
    "erp/sale-order": (
        ("outCount", "已生成关联销售出库单，无法反审批"),
        ("returnCount", "已生成关联销售退货单，无法反审批"),
    ),
    "erp/purchase-order": (
        ("inCount", "已生成关联采购入库单，无法反审批"),
        ("returnCount", "已生成关联采购退货单，无法反审批"),
    ),
    "erp/sale-out": (("receiptPrice", "存在已核销收款金额，无法反审批"),),
    "erp/sale-return": (("refundPrice", "存在已核销退款金额，无法反审批"),),
    "erp/purchase-in": (("paymentPrice", "存在已核销付款金额，无法反审批"),),
    "erp/purchase-return": (("refundPrice", "存在已核销退款金额，无法反审批"),),
}


def _positive_number(value) -> bool:
    if value is None or isinstance(value, bool):
        return False
    try:
        return Decimal(str(value).strip()) > 0
    except (InvalidOperation, ValueError):
        return False


class StatusEffects:
    def __init__(self, operation: str):
        if operation not in {
            "erp_document_status",
            "promotion_coupon_template_status",
            "promotion_seckill_activity_close",
        }:
            raise ValueError(f"unknown status operation: {operation}")
        self.operation = operation

    def project_inputs(self, inputs: dict) -> RequestedEffect:
        if self.operation == "erp_document_status":
            doc_type = _canonical_doc_type(inputs["document_type"])
            entity = ERP_DOCUMENT_ENTITIES[doc_type]
            target_label = "已审核" if inputs["action"] == "审批" else "未审核"
            return RequestedEffect(entity, "state_change", entity, inputs["no"],
                                   canonical({"status": target_label}))
        if self.operation == "promotion_coupon_template_status":
            return RequestedEffect("promotion/coupon-template", "state_change",
                                   "promotion/coupon-template", inputs["target"],
                                   canonical({"status": inputs["status"]}))
        if self.operation == "promotion_seckill_activity_close":
            return RequestedEffect("promotion/seckill-activity", "state_change",
                                   "promotion/seckill-activity", inputs["target"],
                                   canonical({"status": "关闭"}))
        raise ValueError(f"unknown status operation: {self.operation}")


class _StatusHandler:
    def __init__(self, client, operation: str, goal_data, base_factory):
        self.client, self.operation, self.goal_data = client, operation, goal_data
        self.base = base_factory(client, operation, goal_data)

    def _find_erp_doc(self, entity: str, no: str) -> dict:
        result = self.client.get(entity + "/page", no=no, pageNo=1, pageSize=20)
        rows = result.get("list") if isinstance(result, dict) else None
        if not isinstance(rows, list):
            raise BusinessToolRequestError("ERP document list is unavailable")
        exact = [r for r in rows if isinstance(r, dict) and str(r.get("no") or "").strip() == no.strip()]
        if len(exact) != 1:
            choices = tuple(dict.fromkeys(str(r.get("no") or "") for r in rows if isinstance(r, dict) and r.get("no")))[:6]
            raise BusinessToolReferenceError("document is missing or ambiguous; supply its exact no",
                                             entity=entity, choices=choices)
        record = self.client.get(entity + "/get", id=exact[0]["id"])
        if not isinstance(record, dict) or str(record.get("id")) != str(exact[0]["id"]):
            raise BusinessToolRequestError("ERP document detail is unavailable")
        return record

    def _find_by_name(self, entity: str, target: str) -> dict:
        result = self.client.get(entity + "/page", name=target, pageNo=1, pageSize=20)
        rows = result.get("list") if isinstance(result, dict) else None
        if not isinstance(rows, list):
            raise BusinessToolRequestError(f"{entity} list is unavailable")
        exact = [r for r in rows if isinstance(r, dict) and str(r.get("name") or "").strip().casefold() == target.strip().casefold()]
        if len(exact) != 1:
            choices = tuple(dict.fromkeys(str(r.get("name") or "") for r in rows if isinstance(r, dict) and r.get("name")))[:6]
            raise BusinessToolReferenceError("record is missing or ambiguous; supply its exact name",
                                             entity=entity, choices=choices)
        record = self.client.get(entity + "/get", id=exact[0]["id"])
        if not isinstance(record, dict) or str(record.get("id")) != str(exact[0]["id"]):
            raise BusinessToolRequestError(f"{entity} detail is unavailable")
        return record

    def prepare(self, inputs: dict, *, context=None) -> BusinessMutationSpec:
        actor = self.base._actor(context)
        if self.operation == "erp_document_status":
            doc_type = _canonical_doc_type(inputs["document_type"])
            entity = ERP_DOCUMENT_ENTITIES[doc_type]
            record = self._find_erp_doc(entity, inputs["no"])
            target_code = 20 if inputs["action"] == "审批" else 10
            if record.get("status") == target_code:
                raise BusinessMutationRefusedError(
                    f"单据「{inputs['no']}」当前已是{_ERP_STATUS_LABELS[target_code]}状态，无需重复{inputs['action']}。",
                    reason="already_in_target_state",
                )
            if inputs["action"] == "反审批":
                for field, why in _ERP_UNAPPROVE_BLOCKERS.get(entity, ()):
                    if _positive_number(record.get(field)):
                        raise BusinessMutationRefusedError(
                            f"单据「{inputs['no']}」{why}。",
                            reason="downstream_document_exists",
                        )
            before_label = _ERP_STATUS_LABELS.get(record.get("status"), str(record.get("status")))
            after_label = _ERP_STATUS_LABELS[target_code]
            summary = f"{inputs['action']}{doc_type}「{record.get('no')}」"
            diff = ProposalDiff.from_details(
                (str(record.get("no")),),
                (
                    preserved_detail("单据类型", doc_type),
                    preserved_detail("单号", str(record.get("no"))),
                    change_detail("审批状态", before_label, after_label),
                    side_effect_detail("连带业务影响", _ERP_SIDE_EFFECTS[doc_type]),
                ),
            )
            change = RequestedEffect(entity, "state_change", entity, str(record.get("no")), canonical({"status": after_label}))
            preserved = {k: record[k] for k in ("no", "accountId", "totalPrice") if k in record}
            effect = EffectProjection(change, canonical([]), canonical(preserved), canonical({}),
                                      ("record", "unrelated_field_preservation"), preserves_unmentioned=True)
            payload = {
                "actor": str(actor["id"]), "operation": self.operation, "entity": entity,
                "record_id": record["id"], "snapshot": {"id": record["id"], "no": record.get("no"), "status": record.get("status")},
                "preserved": preserved, "method": "PUT", "path": entity + "/update-status",
                "params": {"id": record["id"], "status": target_code}, "body": None,
                "expected": {"status": target_code},
            }
            key = hashlib.sha256(json.dumps({"op": self.operation, "entity": entity, "id": record["id"], "status": target_code},
                                            sort_keys=True).encode()).hexdigest()
            return BusinessMutationSpec(payload, summary, diff.targets,
                                        details=diff.details(), reconcile_key=key,
                                        reconciliation={"entity": entity, "operation": self.operation,
                                                        "id": record["id"], "expected": payload["expected"],
                                                        "before": {"status": record.get("status")}},
                                        effect_projection=effect)

        if self.operation == "promotion_coupon_template_status":
            entity = "promotion/coupon-template"
            record = self._find_by_name(entity, inputs["target"])
            target_code = 0 if inputs["status"] == "开启" else 1
            if record.get("status") == target_code:
                raise BusinessMutationRefusedError(
                    f"优惠券模板「{record.get('name')}」当前已是{inputs['status']}状态。",
                    reason="already_in_target_state",
                )
            before_label = _PROMO_STATUS_LABELS.get(record.get("status"), str(record.get("status")))
            summary = f"{inputs['status']}优惠券模板「{record.get('name')}」"
            diff = ProposalDiff.from_details(
                (str(record.get("name")),),
                (
                    preserved_detail("优惠券模板", str(record.get("name"))),
                    change_detail("开启状态", before_label, inputs["status"]),
                ),
            )
            change = RequestedEffect(entity, "state_change", entity, str(record.get("name")), canonical({"status": inputs["status"]}))
            preserved = {k: record[k] for k in ("name", "discountPrice", "usePrice") if k in record}
            effect = EffectProjection(change, canonical([]), canonical(preserved), canonical({}),
                                      ("record", "unrelated_field_preservation"), preserves_unmentioned=True)
            payload = {
                "actor": str(actor["id"]), "operation": self.operation, "entity": entity,
                "record_id": record["id"], "snapshot": {"id": record["id"], "name": record.get("name"), "status": record.get("status")},
                "preserved": preserved, "method": "PUT", "path": entity + "/update-status",
                "params": None, "body": {"id": record["id"], "status": target_code},
                "expected": {"status": target_code},
            }
            key = hashlib.sha256(json.dumps({"op": self.operation, "id": record["id"], "status": target_code}, sort_keys=True).encode()).hexdigest()
            return BusinessMutationSpec(payload, summary, diff.targets,
                                        details=diff.details(), reconcile_key=key,
                                        reconciliation={"entity": entity, "operation": self.operation,
                                                        "id": record["id"], "expected": payload["expected"],
                                                        "before": {"status": record.get("status")}},
                                        effect_projection=effect)

        entity = "promotion/seckill-activity"
        record = self._find_by_name(entity, inputs["target"])
        if record.get("status") == 1:
            raise BusinessMutationRefusedError(
                f"秒杀活动「{record.get('name')}」已关闭，不能重复关闭。",
                reason="already_in_target_state",
            )
        before_label = _PROMO_STATUS_LABELS.get(record.get("status"), str(record.get("status")))
        summary = f"关闭秒杀活动「{record.get('name')}」"
        diff = ProposalDiff.from_details(
            (str(record.get("name")),),
            (
                preserved_detail("秒杀活动", str(record.get("name"))),
                change_detail("活动状态", before_label, "关闭"),
                side_effect_detail("不可逆影响", "活动关闭后立即停止秒杀且不可重新开启"),
            ),
        )
        change = RequestedEffect(entity, "state_change", entity, str(record.get("name")), canonical({"status": "关闭"}))
        preserved = {k: record[k] for k in ("name", "spuId") if k in record}
        effect = EffectProjection(change, canonical([]), canonical(preserved), canonical({}),
                                  ("record", "unrelated_field_preservation"), preserves_unmentioned=True)
        payload = {
            "actor": str(actor["id"]), "operation": self.operation, "entity": entity,
            "record_id": record["id"], "snapshot": {"id": record["id"], "name": record.get("name"), "status": record.get("status")},
            "preserved": preserved, "method": "PUT", "path": entity + "/close",
            "params": {"id": record["id"]}, "body": None,
            "expected": {"status": 1},
        }
        key = hashlib.sha256(json.dumps({"op": self.operation, "id": record["id"]}, sort_keys=True).encode()).hexdigest()
        return BusinessMutationSpec(payload, summary, diff.targets,
                                    details=diff.details(), reconcile_key=key,
                                    reconciliation={"entity": entity, "operation": self.operation,
                                                    "id": record["id"], "expected": payload["expected"],
                                                    "before": {"status": record.get("status")}},
                                    effect_projection=effect)

    def dispatch(self, args: dict, *, context=None) -> BusinessToolResult:
        entity = args["entity"]
        record_id = args["record_id"]

        def verify_actor():
            actor = self.base._actor(context)
            if str(actor["id"]) != args.get("actor") or args.get("operation") != self.operation:
                raise BusinessToolPermissionError("prepared transition has a different actor or operation")
            return actor

        def pre_dispatch_reread(_actor):
            current = self.client.get(entity + "/get", id=record_id)
            if not isinstance(current, dict) or any(current.get(k) != v for k, v in args["snapshot"].items()):
                raise BusinessMutationRefusedError("record state changed after approval", reason="precondition_changed")
            if self.operation == "erp_document_status" and (args.get("params") or {}).get("status") == 10:
                for field, why in _ERP_UNAPPROVE_BLOCKERS.get(entity, ()):
                    if _positive_number(current.get(field)):
                        raise BusinessMutationRefusedError(why, reason="precondition_changed")
            return current

        def dispatch_once(_actor, _reread_state):
            return self.client.write_once(
                args["method"],
                args["path"],
                body=args["body"],
                params=args["params"],
                operation=self.operation,
            )

        def verify_readback(_actor, _reread_state, _dispatch_result, _uncertain):
            actual = self.client.get(entity + "/get", id=record_id)
            if (not isinstance(actual, dict) or str(actual.get("id")) != str(record_id)
                    or any(actual.get(k) != v for k, v in args["expected"].items())):
                raise BusinessMutationIndeterminate("status transition readback did not confirm target state",
                                                    phase="postcondition", reconciliation={"id": record_id, "entity": entity})
            if any(actual.get(k) != v for k, v in args["preserved"].items()):
                raise BusinessMutationIndeterminate("status transition changed unrelated header fields",
                                                    phase="postcondition", reconciliation={"id": record_id, "entity": entity})
            effects = [{"effect": "record", "verified": True, "id": record_id},
                       {"effect": "unrelated_field_preservation", "verified": True}]
            label = actual.get("no") or actual.get("name") or str(record_id)
            return BusinessToolResult(
                {"verified": True, "record_id": str(record_id), "entity": entity,
                 "reference": f"{label}（ID={record_id}）", "effects": effects},
                {"verified": True, "operation": self.operation, "effects": effects},
            )

        return execute_verified_handler_write(
            verify_actor=verify_actor,
            pre_dispatch_reread=pre_dispatch_reread,
            dispatch_once=dispatch_once,
            verify_readback=verify_readback,
            invalidate_reads=lambda: self.goal_data.invalidate_reads(frozenset({entity})),
            allow_readback_recovery=True,
            use_verification_interval=True,
            readback_failure_message="status transition acknowledged; same-actor readback failed",
            readback_reconciliation=lambda _rs, _dr: {"id": record_id, "entity": entity},
        )


def status_tools(client, goal_data, handler_factory):
    return [
        ToolDefinition(
            "erp_document_status",
            "审批或反审批 ERP 业务单据（采购/销售/出入库/调拨/盘点/收付款单）；不修改明细行或结算账户。",
            json.dumps(object_schema({
                "document_type": {"type": "string", "enum": [*ERP_DOCUMENT_ENTITIES, "库存调度单"]},
                "no": text(100, description="已有单据的确切单号，例如 CGRK20260001；不填内部数字 ID。"),
                "action": {"type": "string", "enum": ["审批", "反审批"]},
            }, ("document_type", "no", "action"))),
            "write", "yudao.write", ToolOperation("erp_document", "update"),
            _StatusHandler(client, "erp_document_status", goal_data, handler_factory),
            reference_inputs=("no",),
            input_fields=(),
            effect_contract=StatusEffects("erp_document_status"),
            scope_entities=tuple(ERP_DOCUMENT_ENTITIES.values()),
            preserves_unmentioned=True,
        ),
        ToolDefinition(
            "promotion_coupon_template_status",
            "开启或关闭商城优惠券模板状态；不修改面额、门槛或数量。",
            json.dumps(object_schema({
                "target": text(200, description="已有优惠券模板的确切名称。"),
                "status": {"type": "string", "enum": ["开启", "关闭"]},
            }, ("target", "status"))),
            "write", "yudao.write", ToolOperation("promotion_coupon_template", "update"),
            _StatusHandler(client, "promotion_coupon_template_status", goal_data, handler_factory),
            reference_inputs=("target",),
            input_fields=(("status", "promotion/coupon-template", "status"),),
            effect_contract=StatusEffects("promotion_coupon_template_status"),
            scope_entities=("promotion/coupon-template",),
            preserves_unmentioned=True,
        ),
        ToolDefinition(
            "promotion_seckill_activity_close",
            "关闭商城秒杀活动（不可逆关闭状态）；不删除活动或修改商品明细。",
            json.dumps(object_schema({
                "target": text(200, description="已有秒杀活动的确切名称。"),
            }, ("target",))),
            "write", "yudao.write", ToolOperation("promotion_seckill_activity", "update"),
            _StatusHandler(client, "promotion_seckill_activity_close", goal_data, handler_factory),
            reference_inputs=("target",),
            input_fields=(),
            effect_contract=StatusEffects("promotion_seckill_activity_close"),
            scope_entities=("promotion/seckill-activity",),
            preserves_unmentioned=True,
        ),
    ]
