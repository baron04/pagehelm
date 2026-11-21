"""Yudao 8-domain capability packs, programmatic write directory, and hub routing helpers."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from web_operation_agent.goal.domain_packs import (
    DomainToolpack,
    build_switch_domain_schema,
    resolve_domain_pack,
    select_active_domains,
)

from ..writes.convention_writes import build_yudao_write_directory

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

_PREFIX_TO_DOMAIN: dict[str, str] = {
    "crm": "crm",
    "erp": "erp_wms",
    "wms": "erp_wms",
    "product": "mall_promotion",
    "trade": "mall_promotion",
    "promotion": "mall_promotion",
    "member": "mall_promotion",
    "statistics": "mall_promotion",
    "fms": "fms_pay",
    "pay": "fms_pay",
    "hrm": "hrm",
    "oa": "oa_bpm",
    "bpm": "oa_bpm",
    "pms": "oa_bpm",
    "im": "oa_bpm",
    "mes": "mes_iot",
    "iot": "mes_iot",
    "system": "system_infra",
    "infra": "system_infra",
    "ai": "system_infra",
    "mp": "system_infra",
}

_WRITE_VERB_TOKENS = frozenset({
    "新建", "创建", "新增", "登记", "添加", "修改", "更新", "编辑", "变更", "调整",
    "审批", "反审批", "审核", "反审", "过账", "开启", "关闭", "启用", "停用", "上架", "下架",
    "分配", "写入", "操作", "提交", "create", "update", "status", "approve", "toggle",
})

YUDAO_DOMAIN_PACKS: tuple[DomainToolpack, ...] = (
    DomainToolpack(
        domain_id="crm",
        label="CRM 客户与回款",
        aliases=("crm", "crm-sales", "客户", "线索", "联系人", "商机", "合同", "回款", "回款计划", "公海", "跟进"),
        route_prefixes=("/crm",),
        write_contract_text=(
            "回款新建调 create_record(entity='crm/receivable')；仅当用户未指定具体期次时，planId 为选填字段可直接省略并在 contractId 名下直接 create_record；"
            "若用户已指定具体回款期次或计划而该计划无权读取、不存在或已关联回款，禁止在同一轮静默省略 planId 或擅自改绑其他期次，更严禁擅自调 crm_receivable_plan_create 代建计划，应如实说明（cannot）或 need_input 请用户确认。"
        ),
        read_contract_text="实体前缀 crm/customer、crm/contact、crm/clue、crm/business、crm/contract、crm/receivable-plan、crm/receivable。",
        default_priority=3,
    ),
    DomainToolpack(
        domain_id="erp_wms",
        label="ERP 进销存与 WMS 仓储",
        aliases=(
            "erp_wms", "erp", "wms", "erp-trade-stock", "erp-wms",
            "采购", "销售", "库存", "仓库", "入库", "出库", "调拨", "盘点", "盘点单", "库存盘点", "库存盘点单",
            "计量单位", "单位", "供应商", "结算账户", "货位", "库位", "货架", "拣货", "波次",
        ),
        route_prefixes=("/erp", "/wms"),
        write_contract_text="ERP 单据审批/反审批优先用 propose_operation('erp_document_status', {document_type,no,action})；计量单位与主数据按契约工具办理。",
        read_contract_text="实体前缀 erp/supplier、erp/customer、erp/warehouse、erp/account、erp/product、erp/product-unit、erp/purchase-*、erp/sale-*、erp/stock-*、wms/*。",
        default_priority=2,
    ),
    DomainToolpack(
        domain_id="mall_promotion",
        label="商城商品交易与营销",
        aliases=(
            "mall_promotion", "mall", "mall-catalog-promo", "mall-trade-aftersale",
            "product", "trade", "promotion", "member",
            "商品", "品牌", "类目", "分类", "规格", "订单", "售后", "退款", "营销", "优惠券", "秒杀", "拼团", "砍价", "商城", "会员", "等级", "标签", "积分",
        ),
        route_prefixes=("/mall", "/product", "/trade", "/promotion", "/member", "/statistics"),
        write_contract_text="已停用支付/退款网关高危动作如实说明安全围栏；品牌/分类/营销状态用契约专用操作或编译写。",
        read_contract_text="实体前缀 product/brand、product/category、product/spu、trade/order、trade/after-sale、promotion/*、member/*。",
        default_priority=1,
    ),
    DomainToolpack(
        domain_id="fms_pay",
        label="财务会计与支付结算",
        aliases=("fms_pay", "fms", "pay", "fms-accounting", "财务", "会计", "凭证", "账套", "科目", "资产负债", "利润表", "现金流", "结账", "支付", "商户", "渠道", "钱包"),
        route_prefixes=("/fms", "/pay"),
        write_contract_text="fms/voucher 等明细修改用 update_record；注意区分日历月与账套当前会计期（year/period）。",
        read_contract_text="实体前缀 fms/config/*、fms/voucher、fms/ledger/*、fms/report/*、pay/*。",
        default_priority=1,
        co_activate_domains=("erp_wms",),
    ),
    DomainToolpack(
        domain_id="hrm",
        label="HRM 人事档案考勤与薪酬",
        aliases=("hrm", "hrm-org-salary", "hrm-salary", "人事", "员工", "入职", "在职", "离职", "花名册", "档案", "薪资", "薪酬", "调薪", "定薪", "工资", "考勤", "打卡", "请假", "排班", "假期", "招聘", "候选人"),
        route_prefixes=("/hrm",),
        write_contract_text=(
            "员工新建用 create_record(entity='hrm/employee')（有页面默认值的字段如 type/entryStatus 未指明可省略自动补齐；在职需 jobNumber）；"
            "调薪/定薪改薪资项用 update_record(entity='hrm/salary/employee-info', target='姓名或姓名(employeeId=编号)', "
            "changes=[主表字段], line_changes=[{'collection':'salaryOptions','match':'薪资项名称或编码','changes':[{'field':'value','value':金额}]}])"
            "（regularSalary/probationSalary 为汇总只读列，勿放 changes；算得目标金额须先调 sql 留回执；"
            "未来日期生效的调薪写入 hrm/salary/change-record（到期前 employee-info 仍显示当前生效薪资，勿误判为未生效））。"
        ),
        read_contract_text="实体前缀 hrm/employee、hrm/salary/employee-info、hrm/salary/*、hrm/attendance/*、hrm/recruit/*。",
        default_priority=1,
        co_activate_domains=("system_infra", "oa_bpm"),
    ),
    DomainToolpack(
        domain_id="oa_bpm",
        label="OA 办公协作与 BPM 审批流",
        aliases=("oa_bpm", "bpm_oa", "oa", "bpm", "pms", "im", "oa-bpm-system", "审批", "流程", "报销", "用车", "出差", "外出", "印章", "公告", "日程", "公文", "督办", "会议", "任务", "工作流", "项目"),
        route_prefixes=("/oa", "/bpm", "/pms", "/im"),
        write_contract_text="未入审批流扁平记录可用 create_record/update_record；流程发起与多行申请草稿走页面表单预填交接。",
        read_contract_text="实体前缀 oa/*、bpm/*、pms/*、im/*（具体实体/必填项可用 search_api 关键词查询）。",
        default_priority=1,
        co_activate_domains=("hrm", "system_infra"),
    ),
    DomainToolpack(
        domain_id="mes_iot",
        label="MES 生产制造与 IoT 物联",
        aliases=("mes_iot", "mes", "iot", "制造", "生产工单", "工单", "质检", "点检", "检验", "工艺路线", "报工", "物料", "车间", "排产", "物联", "物联网", "物联设备"),
        route_prefixes=("/mes", "/iot"),
        write_contract_text="扁平主数据与草稿/待检计划按实体契约用 create_record/update_record；复杂多行工单草稿走页面表单预填。",
        read_contract_text="实体前缀 mes/md/*、mes/pro/*、mes/qc/*、mes/dv/*、mes/wm/*、iot/*（可用 search_api 关键词定位实体）。",
        default_priority=0,
    ),
    DomainToolpack(
        domain_id="system_infra",
        label="系统组织权限与基础设施",
        aliases=("system_infra", "system", "infra", "ai", "mp", "系统", "部门", "岗位", "用户", "角色", "菜单", "字典", "通知", "日志", "定时任务", "租户", "配置", "公众号"),
        route_prefixes=("/system", "/infra", "/ai", "/mp"),
        write_contract_text="system/user 新建含密码硬排除（direct=cannot；仅草稿预填可填非密码项），用户分配角色用 assign_record_relations。",
        read_contract_text=(
            "实体前缀 system/dept、system/post、system/user、system/role、system/menu、system/notice、system/notify-message、system/login-log、infra/*；"
            "注意 system/login-log/page 与 operate-log/page 的 username 按登录账号（如 scale*/admin）而非中文姓名过滤，由人名查日志须先查 system/user/page 取 username。"
        ),
        default_priority=2,
    ),
)

SWITCH_DOMAIN_SCHEMA = build_switch_domain_schema(YUDAO_DOMAIN_PACKS)


def domain_for_entity(entity: str) -> str:
    prefix = str(entity or "").strip().strip("/").split("/", 1)[0].casefold()
    return _PREFIX_TO_DOMAIN.get(prefix, "system_infra" if prefix else "")


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


def _entry_domain(entry: dict) -> str:
    if entry.get("tool") == "erp_document_status":
        return "erp_wms"
    return domain_for_entity(str(entry.get("entity") or ""))


def _terms_from_query(query: str) -> tuple[list[str], str]:
    raw_tokens = [t.strip().casefold() for t in re.split(r"[\s,，、/_\-]+", str(query or "")) if t.strip()]
    subject_tokens = [t for t in raw_tokens if t not in _WRITE_VERB_TOKENS and len(t) >= 2]
    if not subject_tokens:
        subject_tokens = [t for t in raw_tokens if len(t) >= 2]
    intent = ""
    low = str(query or "").casefold()
    if any(w in low for w in ("新建", "创建", "新增", "登记", "添加", "create")):
        intent = "create"
    elif any(w in low for w in ("修改", "更新", "编辑", "调整", "调薪", "update")):
        intent = "update"
    elif any(w in low for w in ("审批", "反审批", "审核", "过账", "开启", "关闭", "启用", "停用", "上架", "下架", "status", "approve")):
        intent = "status"
    elif any(w in low for w in ("分配", "角色", "菜单", "assign")):
        intent = "relation"
    return subject_tokens, intent


def _cjk_bigrams(text: str) -> list[str]:
    runs = re.findall(r"[\u4e00-\u9fff]{3,}", text)
    bigrams: list[str] = []
    seen: set[str] = set()
    for run in runs:
        for i in range(len(run) - 1):
            bg = run[i : i + 2]
            if bg not in seen:
                seen.add(bg)
                bigrams.append(bg)
    return bigrams


def _stripped_label_segments(entry: dict) -> set[str]:
    segments: set[str] = set()
    for raw in (entry.get("tag") or "", entry.get("label") or ""):
        cleaned = str(raw or "").strip().casefold()
        if cleaned.startswith("管理后台 - "):
            cleaned = cleaned[len("管理后台 - "):].strip()
        for part in re.split(r"[\s/（()）|、,，]+", cleaned):
            p = part.strip()
            if p:
                segments.add(p)
    return segments


def search_write_directory(
    query: str,
    *,
    docs: dict | None = None,
    source=None,
    ui=None,
    limit: int = 6,
) -> list[dict]:
    """Rank programmatic write directory entries by subject keyword and write intent."""
    subject_tokens, intent = _terms_from_query(query)
    if not subject_tokens and not intent:
        return []
    directory = build_yudao_write_directory(docs=docs, source=source, ui=ui)
    needed_bigrams = {bg for tok in subject_tokens if re.fullmatch(r"[\u4e00-\u9fff]{3,}", tok) for bg in _cjk_bigrams(tok)}
    bigram_entities: dict[str, set[str]] = {}
    if needed_bigrams:
        for entry in directory:
            lbl_cf = str(entry.get("label") or "").casefold()
            ent_key = str(entry.get("entity") or "")
            for bg in needed_bigrams:
                if bg in lbl_cf:
                    bigram_entities.setdefault(bg, set()).add(ent_key)
    scored: list[tuple[float, dict]] = []
    for entry in directory:
        ent = str(entry.get("entity") or "").casefold()
        lbl = str(entry.get("label") or "").casefold()
        tool = str(entry.get("tool") or "").casefold()
        segments = _stripped_label_segments(entry)
        score = 0.0
        for tok in subject_tokens:
            hit_tok = False
            if tok in segments:
                score += 12.0 + len(tok)
                hit_tok = True
            elif tok in lbl:
                score += 6.0 + len(tok)
                hit_tok = True
            if tok in ent:
                score += 5.0 + len(tok)
                hit_tok = True
            if tok in tool:
                score += 4.0 + len(tok)
                hit_tok = True
            if not hit_tok and re.fullmatch(r"[\u4e00-\u9fff]{3,}", tok):
                bgs = _cjk_bigrams(tok)
                for bg in bgs:
                    if bg in lbl:
                        df = max(1, len(bigram_entities.get(bg, ())))
                        weight = 1.5 if bg == tok[:2] else 1.0
                        score += (6.0 * weight) / (df ** 0.5)
        if score <= 0.0:
            continue
        kind = str(entry.get("kind") or "")
        if intent == "create" and "create" in kind:
            score += 4.0
        elif intent == "update" and "update" in kind:
            score += 4.0
        elif intent == "status" and "status" in kind:
            score += 4.0
        elif intent == "relation" and "relation" in kind:
            score += 4.0
        if entry.get("mode") == "propose_operation":
            score += 0.5
        scored.append((score, entry))
    scored.sort(key=lambda item: (-item[0], str(item[1].get("entity")), str(item[1].get("tool"))))
    return [entry for _sc, entry in scored[:limit]]


def format_write_entry_line(entry: dict) -> str:
    """Format one single-line `[写入契约]` entry for search_api or load_skill hints."""
    kind = str(entry.get("kind") or "")
    tool = str(entry.get("tool") or "")
    entity = str(entry.get("entity") or "")
    label = str(entry.get("label") or entity)
    if kind == "excluded_create":
        return f"[写入边界] {entity} 新建硬排除（{entry.get('reason', '含密禁止代填')}）：仅草稿预填可进页填非密码项，直接建号请报 cannot。"
    if entry.get("mode") == "propose_operation":
        req = ", ".join(entry.get("required") or ())
        if tool == "erp_document_status":
            return f"[写入契约] propose_operation(\"{tool}\", {{\"document_type\": <单据类型>, \"no\": <单号>, \"action\": \"审批|反审批\"}}) — {label}"
        return f"[写入契约] propose_operation(\"{tool}\", {{{req}}}) — {label}（{entity}）"
    if kind == "compiled_create":
        req = ", ".join(entry.get("required") or ()) or "无（均有默认值或均为选填）"
        extra = ""
        defaults = entry.get("defaults") or ()
        if defaults:
            extra += f" | 默认自动补齐(未指明可省略): {', '.join(defaults[:6])}"
        fks = entry.get("relations") or ()
        if fks:
            extra += f" | 外键: {', '.join(fks[:6])}"
        return f"[写入契约] create_record(entity=\"{entity}\", fields=[...]) — {label} | 必填: {req}{extra}（其余未列字段均为选填）"
    if kind == "compiled_aggregate_update":
        cols = "、".join(entry.get("collection_details") or entry.get("collections") or ())
        id_note = f" [id={entry['id_field']}]" if entry.get("id_field") and entry["id_field"] != "id" else ""
        return f"[写入契约] update_record(entity=\"{entity}\", target=..., changes/line_changes=[...]){id_note} — {label} | 子表(line_changes): {cols}"
    if kind == "compiled_update":
        return f"[写入契约] update_record(entity=\"{entity}\", target=..., changes=[...]) — {label}"
    if kind == "compiled_status":
        return f"[写入契约] toggle_record_status(entity=\"{entity}\", target=..., status=\"开启|关闭|上架|下架\") — {label}"
    if kind == "compiled_relation":
        rels = ", ".join(entry.get("relations") or ())
        return f"[写入契约] assign_record_relations(entity=\"{entity}\", target=..., relation=..., items=[...]) — {rels or label}"
    return f"[写入契约] {tool} ({entity}) — {label}"


def _compact_domain_writes(
    domain_id: str,
    *,
    docs: dict | None = None,
    source=None,
    ui=None,
    prompt: str = "",
) -> str:
    entries = [e for e in build_yudao_write_directory(docs=docs, source=source, ui=ui) if _entry_domain(e) == domain_id]
    if not entries:
        return ""
    dedicated = [e for e in entries if e["mode"] == "propose_operation"]
    creates = [e for e in entries if e["kind"] == "compiled_create"]
    updates = [e for e in entries if e["kind"] in {"compiled_aggregate_update", "compiled_update"}]
    statuses = [e for e in entries if e["kind"] == "compiled_status"]
    relations = [e for e in entries if e["kind"] == "compiled_relation"]
    excluded = [e for e in entries if e["kind"] == "excluded_create"]

    prompt_cf = str(prompt or "").casefold()
    segments: list[str] = []
    if dedicated:
        ded_tools = []
        for e in dedicated:
            if e["tool"] == "erp_document_status":
                ded_tools.append("erp_document_status(document_type,no,action=审批|反审批)")
            else:
                ded_tools.append(e["tool"])
        segments.append("propose_operation[" + "、".join(dict.fromkeys(ded_tools)) + "]")

    def _matches_prompt(entry: dict) -> bool:
        if not prompt_cf:
            return False
        if entry["entity"].casefold() in prompt_cf:
            return True
        for seg in _stripped_label_segments(entry):
            if len(seg) >= 2 and seg in prompt_cf:
                return True
        lbl_cf = str(entry.get("label") or "").casefold()
        return any(bg in lbl_cf for bg in _cjk_bigrams(prompt_cf))

    def _format_item(e: dict, *, show_req: bool) -> str:
        ent = e["entity"]
        id_note = f"[id={e['id_field']}]" if e.get("id_field") and e["id_field"] != "id" else ""
        if show_req and e.get("required"):
            return f"{ent}{id_note}({','.join(e['required'][:6])})"
        return f"{ent}{id_note}"

    def _summarize_list(tag: str, items: list[dict], *, show_req: bool = False) -> None:
        if not items:
            return
        if len(items) <= 20:
            segments.append(f"{tag}[" + "、".join(_format_item(e, show_req=show_req) for e in items) + "]")
            return
        prioritized: list[dict] = []
        seen_ents: set[str] = set()
        for e in items:
            if _matches_prompt(e) and e["entity"] not in seen_ents:
                seen_ents.add(e["entity"])
                prioritized.append(e)
        for e in items:
            if e.get("kind") == "compiled_aggregate_update" and e["entity"] not in seen_ents:
                seen_ents.add(e["entity"])
                prioritized.append(e)
        if prioritized:
            shown = "、".join(_format_item(e, show_req=show_req) for e in prioritized[:8])
            segments.append(f"{tag}(命中:{shown}；全域共{len(items)}项，调search_api查全量)")
        else:
            prefixes = sorted({"/".join(e["entity"].split("/")[:2]) for e in items})
            segments.append(f"{tag}(全域{len(items)}项：{'、'.join(prefixes[:8])}；调search_api查精确实体)")

    _summarize_list("create_record", creates, show_req=True)
    _summarize_list("update_record", updates, show_req=False)
    if statuses:
        _summarize_list("toggle_record_status", statuses, show_req=False)
    if relations:
        rel_items = [f"{e['entity']}({','.join(e['relations'][:2])})" for e in relations[:8]]
        segments.append("assign_record_relations[" + "、".join(rel_items) + "]")
    if excluded:
        segments.append("禁API新建[" + "、".join(e["entity"] for e in excluded) + "]")
    return "；".join(segments)


def resolve_yudao_domain_fuzzy(token: str) -> DomainToolpack | None:
    """Resolve exact domain_id/alias or dashed/prefixed pseudo-skill token (e.g. 'hrm-salary')."""
    direct = resolve_domain_pack(YUDAO_DOMAIN_PACKS, token)
    if direct is not None:
        return direct
    raw = str(token or "").strip().casefold()
    if not raw:
        return None
    parts = [p for p in re.split(r"[\s,，、/_\-]+", raw) if p]
    for p in parts:
        mapped = _PREFIX_TO_DOMAIN.get(p)
        if mapped:
            hit = resolve_domain_pack(YUDAO_DOMAIN_PACKS, mapped)
            if hit is not None:
                return hit
        hit = resolve_domain_pack(YUDAO_DOMAIN_PACKS, p)
        if hit is not None:
            return hit
    best: tuple[int, DomainToolpack] | None = None
    for pack in YUDAO_DOMAIN_PACKS:
        score = sum(len(a) for a in (pack.domain_id, pack.label, *pack.aliases) if len(a) >= 2 and a.casefold() in raw)
        if score > 0 and (best is None or score > best[0]):
            best = (score, pack)
    return best[1] if best is not None else None


def format_domain_contract(
    pack: DomainToolpack,
    *,
    writes_enabled: bool = True,
    prompt: str = "",
    docs: dict | None = None,
    source=None,
    ui=None,
) -> str:
    """Render one concise domain pack entry (writes omitted in read-only mode)."""
    parts = [f"- [{pack.domain_id}] {pack.label}"]
    if pack.read_contract_text:
        parts.append(pack.read_contract_text)
    if writes_enabled:
        if pack.write_contract_text:
            parts.append(pack.write_contract_text)
        prog_writes = _compact_domain_writes(pack.domain_id, docs=docs, source=source, ui=ui, prompt=prompt)
        if prog_writes:
            parts.append("可用写入：" + prog_writes)
    return "：".join(parts[:2]) + (" " + " ".join(parts[2:]) if len(parts) > 2 else "")


def active_domain_contract_block(
    *,
    page_url: str = "",
    prompt: str = "",
    pinned_domains: tuple[str, ...] = (),
    writes_enabled: bool = True,
    top_k: int = 2,
    max_total: int = 3,
    docs: dict | None = None,
    source=None,
    ui=None,
) -> str:
    """Build compact Top-K active-domain guidance injected dynamically per turn."""
    selected = select_active_domains(
        YUDAO_DOMAIN_PACKS,
        page_url=page_url,
        prompt=prompt,
        pinned_domains=pinned_domains,
        top_k=max(2, int(top_k)),
        max_total=max(3, int(max_total)),
    )
    if not selected:
        return ""
    lines = [YUDAO_ACTIVE_DOMAIN_HEADER]
    for pack in selected:
        lines.append(
            format_domain_contract(
                pack,
                writes_enabled=writes_enabled,
                prompt=prompt,
                docs=docs,
                source=source,
                ui=ui,
            )
        )
    return "\n".join(lines)


__all__ = [
    "SWITCH_DOMAIN_SCHEMA",
    "YUDAO_ACTIVE_DOMAIN_HEADER",
    "YUDAO_DEFERRED_DOM_TOOLS",
    "YUDAO_DOMAIN_PACKS",
    "active_domain_contract_block",
    "domain_for_entity",
    "format_domain_contract",
    "format_write_entry_line",
    "is_yudao_hub_route",
    "resolve_domain_pack",
    "resolve_yudao_domain_fuzzy",
    "search_write_directory",
]
