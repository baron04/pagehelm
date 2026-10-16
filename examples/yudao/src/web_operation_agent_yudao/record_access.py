"""Application-owned CRM record edit facts; these facts grant no effect authority."""
from web_operation_agent.business.errors import BusinessToolContractError


def _record_can_edit(identity, team, record, actor, *, biz_type, permission):
    permissions = identity.get("permissions") if isinstance(identity, dict) else None
    if (not isinstance(permissions, list) or any(not isinstance(p, str) for p in permissions)
            or not isinstance(team, list) or any(not isinstance(row, dict) for row in team)):
        raise BusinessToolContractError("the CRM permission response is malformed")
    return bool({permission, "*:*:*"}.intersection(permissions)) and any(
        str(row.get("userId")) == actor and type(row.get("bizType")) is int and row["bizType"] == biz_type
        and str(row.get("bizId")) == record and type(row.get("level")) is int
        and row["level"] in (1, 3) for row in team)


def customer_can_edit(identity, team, record, actor):
    return _record_can_edit(identity, team, record, actor, biz_type=2, permission="crm:customer:update")


def contact_can_edit(identity, team, record, actor):
    return _record_can_edit(identity, team, record, actor, biz_type=3, permission="crm:contact:update")
