"""Actor-read relation names and hierarchy paths; no model-provided IDs."""
from dataclasses import asdict
from collections import Counter
import re

from web_operation_agent.business.errors import (BusinessToolRequestError,
    BusinessToolReferenceError, BusinessMutationRefusedError)
from .write_definitions import RecordRelation


def resolve_relation(client, contract, display):
    if contract.root and display == "根节点":
        return {**asdict(contract), "id": 0, "name": "根节点", "display": display, "snapshot": {}}
    rows = client.get(contract.list_path)
    if not isinstance(rows, list) or len(rows) > 20000 or any(
            not isinstance(r, dict) or type(r.get("id")) is not int or
            not isinstance(r.get(contract.name_field), str) for r in rows):
        raise BusinessToolRequestError("current relation list is invalid or exceeds its bound")
    by_id = {r["id"]: r for r in rows}
    if len(by_id) != len(rows):
        raise BusinessToolRequestError("current relation list has duplicate identities")
    def path(row):
        parts, visited = [], set()
        while row:
            if row["id"] in visited or len(visited) >= 16:
                raise BusinessToolRequestError("current relation hierarchy is invalid")
            visited.add(row["id"])
            parts.append(row[contract.name_field])
            parent = row.get("parentId")
            if parent and parent not in by_id:
                raise BusinessToolRequestError("current relation hierarchy is incomplete")
            row = by_id.get(parent)
        return "/".join(reversed(parts))
    candidates = []
    for row in rows:
        name = row[contract.name_field]
        shown = f"{name}（{row['deptName']}）" if contract.name_field == "nickname" and row.get("deptName") else path(row)
        candidates.append((row, shown))
    counts = Counter(shown for _, shown in candidates)
    candidates = [(row, f"{shown}（ID={row['id']}）" if counts[shown] > 1 else shown)
                  for row, shown in candidates]
    reference = re.fullmatch(r"(.+)（ID=([0-9]+)）", display)
    exact = [(row, shown) for row, shown in candidates if display in (row[contract.name_field], shown)
        or reference and str(row["id"]) == reference.group(2) and
        reference.group(1) in (row[contract.name_field], shown)]
    if len(exact) != 1:
        choices = tuple(dict.fromkeys(shown for row, shown in candidates
            if display in row[contract.name_field] or row[contract.name_field] in display))[:6]
        raise BusinessToolReferenceError("relation is missing or ambiguous; use its current complete name",
            entity=contract.entity, choices=choices)
    row, _ = exact[0]
    return {**asdict(contract), "id": row["id"], "name": row[contract.name_field], "display": display,
            "snapshot": {k: row[k] for k in (contract.name_field, "parentId", "status", "deptId", "deptName") if k in row}}


def recheck_relation(client, relation):
    contract = RecordRelation(**{k: relation[k] for k in RecordRelation.__dataclass_fields__})
    try:
        current = resolve_relation(client, contract, relation["display"])
    except BusinessToolRequestError as exc:
        raise BusinessMutationRefusedError("related choice changed after approval; prepare again",
            reason="precondition_changed") from exc
    if current["id"] != relation["id"] or current["snapshot"] != relation["snapshot"]:
        raise BusinessMutationRefusedError("related choice changed after approval; prepare again",
            reason="precondition_changed")
