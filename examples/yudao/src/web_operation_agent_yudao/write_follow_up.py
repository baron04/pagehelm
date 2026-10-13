"""Reviewed follow-up effects of each native CRM target, shared by all phases."""
from dataclasses import dataclass
from types import MappingProxyType

from web_operation_agent.business.errors import BusinessToolContractError, BusinessToolRequestError


@dataclass(frozen=True)
class FollowUpTarget:
    biz_type: int
    copied_fields: tuple[tuple[str, str], ...] = ()
    marks_followed: bool = False

    @property
    def fields(self):
        return ("contactLastTime", *(field for field, _ in self.copied_fields),
                *(("followUpStatus",) if self.marks_followed else ()))

    def require_readable(self, record):
        if not isinstance(record, dict) or any(field not in record for field in self.fields):
            raise BusinessToolContractError("the native target does not expose its required follow-up effects")

    def values(self, body):
        return {**{field: body[source] for field, source in self.copied_fields},
                **({"followUpStatus": True} if self.marks_followed else {})}

    def projection(self, body):
        return {**self.values(body), "contactLastTime": "application_execution_time"}

    def approval_text(self):
        changes = ["最近联系时间由应用设为执行时间"]
        labels = {"contactNextTime": "下次联系时间", "contactLastContent": "最近跟进内容"}
        changes.extend(labels[field] + "改为本记录的值" for field, _ in self.copied_fields)
        if self.marks_followed:
            changes.append("标记已跟进")
        return "；".join(changes) + "。"

    def verified(self, record, body, started_ms, ended_ms):
        if not isinstance(record, dict) or record.get("id") != body["bizId"]:
            return False
        values = self.values(body)
        if any(field not in record or type(record[field]) is not type(value) or record[field] != value
               for field, value in values.items()):
            return False
        timestamp = record.get("contactLastTime")
        return type(timestamp) is int and started_ms - 5000 <= timestamp <= ended_ms + 5000


# The native services differ: business omits last content, contact has no
# followed flag, and contract updates only the execution timestamp.
FOLLOW_UP_TARGETS = MappingProxyType({
    "crm/customer": FollowUpTarget(2, (("contactNextTime", "nextTime"), ("contactLastContent", "content")), True),
    "crm/clue": FollowUpTarget(1, (("contactNextTime", "nextTime"), ("contactLastContent", "content")), True),
    "crm/contact": FollowUpTarget(3, (("contactNextTime", "nextTime"), ("contactLastContent", "content"))),
    "crm/business": FollowUpTarget(4, (("contactNextTime", "nextTime"),), True),
    "crm/contract": FollowUpTarget(5),
})


def follow_up_target(entity):
    try:
        return FOLLOW_UP_TARGETS[entity]
    except (KeyError, TypeError) as exc:
        raise BusinessToolRequestError("follow-up target type is not installed") from exc
