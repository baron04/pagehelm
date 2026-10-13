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


def _area(client, label):
    rows = client.get("system/area/tree")
    if not isinstance(rows, list):
        raise BusinessToolRequestError("current region tree is unavailable")
    found = []
    count = 0
    def visit(items, prefix=(), depth=0):
        nonlocal count
        if depth > 8 or not isinstance(items, list):
            raise BusinessToolRequestError("region tree exceeds its bounds")
        for row in items:
            count += 1
            if count > 20000 or not isinstance(row, dict) or not isinstance(row.get("name"), str):
                raise BusinessToolRequestError("region tree is invalid or exceeds its bounds")
            path = (*prefix, row["name"])
            if label in (row["name"], "/".join(path)):
                found.append(row)
            visit(row.get("children") or [], path, depth + 1)
    visit(rows)
    if len(found) != 1 or type(found[0].get("id")) is not int:
        raise BusinessToolRequestError("region is missing or ambiguous; provide the full province/city/district path")
    return found[0]["id"]


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
