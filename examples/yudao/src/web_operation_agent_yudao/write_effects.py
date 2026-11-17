"""Reviewed business effects and native value decoding; no model interpretation."""
from datetime import datetime
from zoneinfo import ZoneInfo

from web_operation_agent.business.effects import RequestedEffect, EffectProjection, canonical
from web_operation_agent.business.errors import BusinessToolRequestError
from .write_definitions import RECORD_OPERATIONS
from .write_follow_up import follow_up_target


class NativeEffects:
    def __init__(self, operation):
        self.operation = operation

    def project_inputs(self, inputs):
        contract = RECORD_OPERATIONS.get(self.operation)
        if contract:
            update = self.operation.endswith("update")
            values = dict(inputs["changes"] if update else inputs)
            aliases = {"owner": "ownerUserId", "customer": "customerId", **{r.input: r.field for r in contract.relations}}
            values = {aliases.get(k, k): v for k, v in values.items()}
            return RequestedEffect(contract.entity, "update" if update else "create",
                contract.entity if update else "", inputs["target"] if update else "", canonical(values))
        follow_up_target(inputs["entity"])
        values = {k: inputs[k] for k in ("type", "content", "nextTime")}
        for path, field in (("contacts", "contactIds"), ("businesses", "businessIds")):
            if path in inputs:
                values[field] = inputs[path]
        return RequestedEffect("crm/follow-up-record", "create", inputs["entity"],
            inputs["target"], canonical(values))


def prepared_effect(operation, inputs, body, *, actor, choices, relations, snapshot, goal_data):
    declared = NativeEffects(operation).project_inputs(inputs)
    contract = RECORD_OPERATIONS.get(operation)
    values = {}
    selected = {c["field"]: c for c in choices}
    zone = getattr(goal_data, "time_zone", "Asia/Shanghai")
    zone = zone() if callable(zone) else zone
    for field, requested in declared.values.items():
        actual = body[field]
        if field in selected:
            choice = selected[field]
            if actual != choice["code"]:
                raise BusinessToolRequestError("prepared choice does not match its reviewed encoding")
            actual = choice["label"]
        elif field in {"contactNextTime", "nextTime"}:
            actual = datetime.fromtimestamp(actual / 1000, ZoneInfo(zone)).strftime("%Y-%m-%d %H:%M:%S")
        elif field == "qq" and contract and contract.prefix == "crm_contact":
            actual = str(actual)
        elif field == "status":
            actual = {0: "开启", 1: "关闭"}[actual]
        elif field == "ownerUserId":
            bound = [r for r in relations if r.get("field") == field]
            if bound:
                if len(bound) != 1 or bound[0]["id"] != actual or bound[0]["display"] != requested:
                    raise BusinessToolRequestError("prepared owner does not match its resolved name")
            elif str(actual) != str(actor["id"]):
                raise BusinessToolRequestError("prepared owner does not match the current actor")
            actual = requested
        elif field == "customerId":
            if not any(r["entity"] == "crm/customer" and r["id"] == actual for r in relations):
                raise BusinessToolRequestError("prepared related customer is not resolved")
            actual = requested
        elif contract and field in {r.field for r in contract.relations}:
            bound = [r for r in relations if r.get("field") == field]
            if len(bound) != 1 or bound[0]["id"] != actual or bound[0]["display"] != requested:
                raise BusinessToolRequestError("prepared relation does not match its resolved name")
            actual = requested
        elif field in {"contactIds", "businessIds"}:
            resolved = [r for r in relations if r.get("field") == field]
            if (len(actual) != len(resolved) or set(actual) != {r["id"] for r in resolved}
                    or set(requested) != {r["display"] for r in resolved}):
                raise BusinessToolRequestError("prepared relation set differs from the complete requested set")
            actual = requested
        elif field == "type" and not contract:
            # Dictionary label/code has been read under the actor during prepare.
            actual = inputs["type"]
        values[field] = actual
    bindings = list(relations)
    if snapshot:
        bindings.append({"entity": declared.target_entity, "id": snapshot["id"],
                         "name": snapshot[contract.name_field if contract else "name"]})
    preserved = {k: snapshot[k] for k in (contract.preserved if contract else ())
                 if k in snapshot and k not in values}
    duties = ["record"]
    derived = {k: v for k, v in body.items() if k not in values and k != "id"}
    if contract and contract.owner_type and operation.endswith("create"):
        duties.append("owner_permission")
        derived["owner_permission"] = {"actor": str(body["ownerUserId"]), "level": "OWNER"}
    if preserved:
        duties.append("unrelated_field_preservation")
    if not contract:
        duties.append("main_follow_up")
        derived["main_follow_up"] = follow_up_target(inputs["entity"]).projection(body)
        linked = [r for r in relations if r.get("field") in {"contactIds", "businessIds"}]
        if linked:
            duties.append("related_follow_up")
            derived["related_follow_up"] = {"targets": linked, "nextTime": body["nextTime"]}
    change = RequestedEffect(declared.entity, declared.action, declared.target_entity,
                             declared.target, canonical(values))
    before = ({k: snapshot.get(k) for k in values if k in snapshot}
              if snapshot and declared.action == "update" else {})
    return EffectProjection(change, canonical(bindings), canonical(preserved),
                            canonical(derived), tuple(duties),
                            preserves_unmentioned=bool(contract and operation.endswith("update")),
                            before_json=canonical(before))
