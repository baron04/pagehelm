"""Reviewed label, region and datetime encoding for native CRM requests."""
from datetime import datetime
import re
from zoneinfo import ZoneInfo

from web_operation_agent.business.errors import (BusinessToolRequestError,
                                                BusinessMutationRefusedError)
from web_operation_agent.schemas.approval_details import ApprovalDetail

CUSTOMER_DICTIONARIES = {"industryId": "crm_customer_industry",
                         "source": "crm_customer_source", "level": "crm_customer_level"}
CONTACT_DICTIONARIES = {"sex": "system_user_sex"}


def epoch(value, goal_data):
    zone = getattr(goal_data, "time_zone", "Asia/Shanghai")
    zone = zone() if callable(zone) else zone
    try:
        date = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo(zone))
    except ValueError as exc:
        raise BusinessToolRequestError("contact time must be YYYY-MM-DD HH:MM:SS") from exc
    return int(date.timestamp() * 1000)


_MUNICIPALITY_ALIASES = {
    "北京": "北京市",
    "上海": "上海市",
    "天津": "天津市",
    "重庆": "重庆市",
}
_PROVINCE_ALIASES = {
    **_MUNICIPALITY_ALIASES,
    "河北": "河北省", "山西": "山西省", "辽宁": "辽宁省", "吉林": "吉林省", "黑龙江": "黑龙江省",
    "江苏": "江苏省", "浙江": "浙江省", "安徽": "安徽省", "福建": "福建省", "江西": "江西省",
    "山东": "山东省", "河南": "河南省", "湖北": "湖北省", "湖南": "湖南省", "广东": "广东省",
    "海南": "海南省", "四川": "四川省", "贵州": "贵州省", "云南": "云南省", "陕西": "陕西省",
    "甘肃": "甘肃省", "青海": "青海省", "台湾": "台湾省",
    "内蒙古": "内蒙古自治区", "广西": "广西壮族自治区", "西藏": "西藏自治区",
    "宁夏": "宁夏回族自治区", "新疆": "新疆维吾尔自治区",
    "香港": "香港特别行政区", "澳门": "澳门特别行政区",
}


def _normalize_area_label(raw_label: str) -> str:
    parts = [p.strip() for p in re.split(r"[/\s]+", str(raw_label or "").strip()) if p.strip()]
    if not parts:
        return str(raw_label or "").strip()
    parts[0] = _PROVINCE_ALIASES.get(parts[0], parts[0])
    if len(parts) >= 2 and parts[1] in _MUNICIPALITY_ALIASES:
        parts[1] = _MUNICIPALITY_ALIASES[parts[1]]
    return "/".join(parts)


def _area(client, label):
    rows = client.get("system/area/tree")
    if not isinstance(rows, list):
        raise BusinessToolRequestError("current region tree is unavailable")
    norm = _normalize_area_label(label)
    raw = str(label or "").strip()
    found: list[tuple[dict, tuple[str, ...], dict | None]] = []
    count = 0

    def visit(items, prefix=(), parent=None, depth=0):
        nonlocal count
        if depth > 8 or not isinstance(items, list):
            raise BusinessToolRequestError("region tree exceeds its bounds")
        for row in items:
            count += 1
            if count > 20000 or not isinstance(row, dict) or not isinstance(row.get("name"), str):
                raise BusinessToolRequestError("region tree is invalid or exceeds its bounds")
            path = (*prefix, row["name"])
            dedup_path = tuple(
                seg for idx, seg in enumerate(path) if idx == 0 or seg != path[idx - 1]
            )
            candidates = {row["name"], "/".join(path), "/".join(dedup_path)}
            if raw in candidates or norm in candidates:
                found.append((row, path, parent))
            visit(row.get("children") or [], path, row, depth + 1)

    visit(rows)
    if not found and "/" not in norm and not norm.endswith(("省", "市", "区", "县", "州", "盟", "旗")):
        city_norm = norm + "市"
        visit_hits: list[dict] = []
        def visit_city(items, depth=0):
            if depth > 8 or not isinstance(items, list):
                return
            for row in items:
                if isinstance(row, dict) and row.get("name") == city_norm:
                    visit_hits.append(row)
                if isinstance(row, dict):
                    visit_city(row.get("children") or [], depth + 1)
        visit_city(rows)
        if len(visit_hits) == 1 and type(visit_hits[0].get("id")) is int:
            return visit_hits[0]["id"]
    if len(found) == 2:
        (r0, p0, par0), (r1, p1, par1) = found
        if par1 is r0 and r0.get("name") == r1.get("name") and "/" not in norm:
            found = [(r0, p0, par0)]
        elif par0 is r1 and r0.get("name") == r1.get("name") and "/" not in norm:
            found = [(r1, p1, par1)]
    if len(found) != 1 or type(found[0][0].get("id")) is not int:
        raise BusinessToolRequestError("region is missing or ambiguous; provide the full province/city/district path")
    return found[0][0]["id"]


def encode(client, goal_data, values, *, dictionaries=(), numeric_qq=False):
    body, details, choices = dict(values), [], []
    dictionary_types = dict(dictionaries)
    dictionary_rows = client.get("system/dict-data/simple-list") if any(k in values for k in dictionary_types) else []
    if not isinstance(dictionary_rows, list):
        raise BusinessToolRequestError("current dictionary is unavailable")
    for field, kind in dictionary_types.items():
        if field not in body:
            continue
        label = body[field]
        matches = [r for r in dictionary_rows if isinstance(r, dict)
                   and r.get("dictType") == kind and r.get("label") == label]
        if len(matches) != 1 or not re.fullmatch(r"-?\d+", str(matches[0].get("value", ""))):
            raise BusinessToolRequestError("a choice must match one current dictionary label")
        body[field] = int(matches[0]["value"])
        choices.append({"field": field, "type": kind, "label": label, "code": body[field]})
        details.append(ApprovalDetail("change", field, label))
    if "areaId" in body:
        label = body["areaId"]
        details.append(ApprovalDetail("change", "areaId", label))
        body["areaId"] = _area(client, label)
        choices.append({"field": "areaId", "type": "area", "label": label, "code": body["areaId"]})
    if "contactNextTime" in body:
        details.append(ApprovalDetail("change", "contactNextTime", body["contactNextTime"]))
        body["contactNextTime"] = epoch(body["contactNextTime"], goal_data)
    if numeric_qq and "qq" in body:
        if not re.fullmatch(r"0|[1-9]\d{0,18}", body["qq"]) or int(body["qq"]) > 2**63-1:
            raise BusinessToolRequestError("contact QQ must be a canonical nonnegative 64-bit number")
        body["qq"] = int(body["qq"])
    if "status" in body:
        details.append(ApprovalDetail("change", "status", body["status"]))
        body["status"] = {"开启": 0, "关闭": 1}[body["status"]]
    return body, details, choices


def recheck_choices(client, choices):
    if not choices:
        return
    rows = client.get("system/dict-data/simple-list") if any(c["type"] != "area" for c in choices) else []
    if not isinstance(rows, list):
        raise BusinessMutationRefusedError("dictionary is unavailable after approval", reason="precondition_changed")
    for choice in choices:
        if choice["type"] == "area":
            try:
                unchanged = _area(client, choice["label"]) == choice["code"]
            except BusinessToolRequestError:
                unchanged = False
            if not unchanged:
                raise BusinessMutationRefusedError("region changed after approval; prepare again", reason="precondition_changed")
            continue
        matches = [r for r in rows if isinstance(r, dict) and r.get("dictType") == choice["type"]
                   and r.get("label") == choice["label"]]
        if len(matches) != 1 or str(matches[0].get("value")) != str(choice["code"]):
            raise BusinessMutationRefusedError("dictionary changed after approval; prepare again", reason="precondition_changed")
