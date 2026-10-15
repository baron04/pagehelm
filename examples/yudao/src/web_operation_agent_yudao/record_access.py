"""Application-owned customer edit facts; these facts grant no effect authority."""
from web_operation_agent.business.errors import BusinessToolContractError


def customer_can_edit(identity, team, record, actor):
    permissions = identity.get("permissions") if isinstance(identity, dict) else None
    if (not isinstance(permissions, list) or any(not isinstance(p, str) for p in permissions)
            or not isinstance(team, list) or any(not isinstance(row, dict) for row in team)):
        raise BusinessToolContractError("the customer permission response is malformed")
    return bool({"crm:customer:update", "*:*:*"}.intersection(permissions)) and any(
        str(row.get("userId")) == actor and type(row.get("bizType")) is int and row["bizType"] == 2
        and str(row.get("bizId")) == record and type(row.get("level")) is int
        and row["level"] in (1, 3) for row in team)
