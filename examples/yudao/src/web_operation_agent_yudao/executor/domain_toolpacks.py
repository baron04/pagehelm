"""Yudao domain capability packs, dynamic write-contract rendering, and portal hub detection."""
from __future__ import annotations

from urllib.parse import urlsplit

from web_operation_agent.goal.domain_packs import (
    DomainToolpack,
    build_switch_domain_schema,
    resolve_domain_pack,
    select_active_domains,
)

YUDAO_ACTIVE_DOMAIN_HEADER = "当前激活业务域契约（仅覆盖本轮命中域；跨域时可调 switch_domain 追加）："
YUDAO_DEFERRED_DOM_TOOLS = frozenset({
    "click",
    "fill_fields",
    "input",
    "select",
    "scroll",
    "highlight",
    "clear_date",
})

YUDAO_DOMAIN_PACKS: tuple[DomainToolpack, ...] = (
    DomainToolpack(
        domain_id="crm",
        label="CRM 客户与回款",
        aliases=("crm", "crm-sales", "客户", "线索", "联系人", "商机", "合同", "回款", "回款计划", "公海", "跟进"),
        route_prefixes=("/crm",),
        write_contract_text=(
            "专用写入（propose_operation）：crm_customer_create/update、crm_contact_create/update、"
            "crm_clue_create/update、crm_follow_up_create、crm_receivable_plan_create、"
            "crm_customer_put_pool、crm_customer_receive、crm_customer_lock、crm_customer_deal_status。"
            "常驻工具：create_record（扁平新建如 crm/receivable；期次外键支持 父名称/第N期 或已读候选ID）/ update_record。"
        ),
        read_contract_text="主要实体前缀 crm/customer、crm/contact、crm/clue、crm/business、crm/contract、crm/receivable-plan、crm/receivable。",
        default_priority=3,
    ),
    DomainToolpack(
        domain_id="erp",
        label="ERP 进销存与财务",
        aliases=("erp", "erp-trade-stock", "erp-finance", "fms", "fms-accounting", "财务", "会计", "采购", "销售", "库存", "仓库", "出库", "入库", "调拨", "盘点", "收款", "付款", "供应商", "结算账户"),
        route_prefixes=("/erp", "/fms"),
        write_contract_text=(
            "专用写入（propose_operation）：erp_supplier_create/update、erp_customer_create/update、"
            "erp_warehouse_create/update、erp_account_create/update、erp_document_status（审批/反审批）。"
            "主数据与扁平记录可用 create_record / update_record / toggle_record_status；多行单据起草走页面表单预填交接。"
        ),
        read_contract_text="主要实体前缀 erp/supplier、erp/customer、erp/warehouse、erp/account、erp/product、erp/stock、erp/purchase-*、erp/sale-*、fms/*。",
        default_priority=2,
    ),
    DomainToolpack(
        domain_id="bpm_oa",
        label="BPM 流程与 OA 办公",
        aliases=("bpm", "oa", "oa-bpm-system", "审批", "流程", "请假", "报销", "用车", "出差", "外出", "印章", "公告", "日程", "公文", "督办", "会议", "任务", "工作流"),
        route_prefixes=("/bpm", "/oa"),
        write_contract_text=(
            "专用写入（propose_operation）：bpm_category_create、oa_announcement_create。"
            "未进审批流的扁平记录可用 create_record / update_record；已入流程记录不可直接修改，流程发起与多行申请走页面表单预填交接。"
        ),
        read_contract_text="主要实体前缀 oa/announcement、oa/*-apply、oa/meeting、bpm/category、bpm/process-instance。",
        default_priority=2,
    ),
    DomainToolpack(
        domain_id="mall",
        label="商城商品交易与营销",
        aliases=("mall", "mall-catalog-promo", "mall-trade-aftersale", "商品", "品牌", "类目", "规格", "订单", "售后", "营销", "优惠券", "秒杀", "拼团", "砍价", "商城"),
        route_prefixes=("/mall", "/product", "/trade", "/promotion", "/statistics"),
        write_contract_text=(
            "专用写入（propose_operation）：product_brand_create/update、product_category_create、"
            "promotion_coupon_template_status、promotion_seckill_activity_close。"
            "扁平档案与启停/上下架可用 create_record / update_record / toggle_record_status。"
        ),
        read_contract_text="主要实体前缀 product/brand、product/category、product/spu、trade/order、trade/after-sale、promotion/*。",
        default_priority=1,
    ),
    DomainToolpack(
        domain_id="system_infra",
        label="系统组织人事与基础设施",
        aliases=("system", "infra", "hrm-org-salary", "系统", "部门", "岗位", "用户", "角色", "菜单", "字典", "通知", "日志", "定时任务", "租户", "配置", "薪酬", "考勤"),
        route_prefixes=("/system", "/infra", "/hrm"),
        write_contract_text=(
            "专用写入（propose_operation）：system_dept_create/update、system_post_create/update、system_notice_create。"
            "常驻工具：create_record（不含密码新建）、update_record、toggle_record_status、assign_record_relations（如用户角色分配）；含密码新建走页面表单预填交接。"
        ),
        read_contract_text="主要实体前缀 system/dept、system/post、system/user、system/role、system/notice、system/dict-*、infra/*。",
        default_priority=1,
    ),
    DomainToolpack(
        domain_id="mes",
        label="MES 生产制造与质检",
        aliases=("mes", "生产", "工单", "质检", "点检", "设备", "检验", "工艺", "报工", "物料", "车间", "排产"),
        route_prefixes=("/mes",),
        write_contract_text=(
            "常驻工具：create_record / update_record / toggle_record_status（扁平主数据与草稿/待检计划，已启用点检计划限制详见 entity_rules）；"
            "复杂工艺路线/多行工单新建走页面表单预填交接。"
        ),
        read_contract_text="主要实体前缀 mes/md/*、mes/pro/*、mes/qc/*、mes/dv/*。",
        default_priority=0,
    ),
    DomainToolpack(
        domain_id="wms",
        label="WMS 仓储作业与库位",
        aliases=("wms", "仓储", "库位", "货架", "拣货", "波次", "库存移动", "盘点方案", "盘点单"),
        route_prefixes=("/wms",),
        write_contract_text=(
            "常驻工具：create_record / update_record / toggle_record_status（wms/item 档案及草稿态 WMS 单据，非草稿单据及盘点方案状态限制详见 entity_rules）。"
        ),
        read_contract_text="主要实体前缀 wms/item、wms/warehouse、wms/area、wms/*-order、wms/stocktaking-plan。",
        default_priority=0,
    ),
    DomainToolpack(
        domain_id="member_pay_ai_iot",
        label="会员支付与 AI/IoT",
        aliases=("member", "pay", "ai", "iot", "mp", "会员", "等级", "标签", "分组", "积分", "支付", "商户", "渠道", "物联网", "设备物联", "公众号", "知识库"),
        route_prefixes=("/member", "/pay", "/ai", "/iot", "/mp"),
        write_contract_text=(
            "专用写入（propose_operation）：member_tag_create、member_group_create。"
            "常驻工具：create_record / update_record / toggle_record_status / assign_record_relations。"
        ),
        read_contract_text="主要实体前缀 member/*、pay/*、ai/*、iot/*、mp/*。",
        default_priority=0,
    ),
)

SWITCH_DOMAIN_SCHEMA = build_switch_domain_schema(YUDAO_DOMAIN_PACKS)


def is_yudao_hub_route(obs) -> bool:
    """True when the page observation is on the portal hub with no open dialog/drawer form."""
    if obs is None:
        return False
    elements = getattr(obs, "elements", ()) or ()
    for el in elements:
        attrs = getattr(el, "attrs", {}) or {}
        if attrs.get("form_scope") == "dialog" or getattr(el, "role", "") == "dialog":
            return False
    raw_route = str(getattr(obs, "page_route", "") or "").strip()
    if not raw_route:
        raw_url = str(getattr(obs, "url", "") or "").strip()
        parsed = urlsplit(raw_url)
        frag = parsed.fragment or ""
        raw_route = frag.split("?", 1)[0] if frag.startswith("/") else parsed.path
    norm = raw_route.split("?", 1)[0].rstrip("/") or "/"
    return norm in {"/", "/index"}


def format_domain_contract(pack: DomainToolpack, *, writes_enabled: bool = True) -> str:
    """Render one concise domain pack entry (writes omitted in read-only mode)."""
    parts = [f"- [{pack.domain_id}] {pack.label}"]
    if pack.read_contract_text:
        parts.append(pack.read_contract_text)
    if writes_enabled and pack.write_contract_text:
        parts.append(pack.write_contract_text)
    return "：".join(parts[:2]) + (" " + " ".join(parts[2:]) if len(parts) > 2 else "")


def active_domain_contract_block(
    *,
    page_url: str = "",
    prompt: str = "",
    pinned_domains: tuple[str, ...] = (),
    writes_enabled: bool = True,
    top_k: int = 2,
) -> str:
    """Build compact Top-K active-domain guidance injected dynamically per turn."""
    selected = select_active_domains(
        YUDAO_DOMAIN_PACKS,
        page_url=page_url,
        prompt=prompt,
        pinned_domains=pinned_domains,
        top_k=max(2, int(top_k)),
    )
    if not selected:
        return ""
    lines = [YUDAO_ACTIVE_DOMAIN_HEADER]
    for pack in selected:
        lines.append(format_domain_contract(pack, writes_enabled=writes_enabled))
    return "\n".join(lines)


__all__ = [
    "SWITCH_DOMAIN_SCHEMA",
    "YUDAO_ACTIVE_DOMAIN_HEADER",
    "YUDAO_DEFERRED_DOM_TOOLS",
    "YUDAO_DOMAIN_PACKS",
    "active_domain_contract_block",
    "format_domain_contract",
    "is_yudao_hub_route",
    "resolve_domain_pack",
]
