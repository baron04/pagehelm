"""Compare independently declared requirements with proposed inputs, before writes.

Both declarations can misunderstand the person. This detects disagreement and
omission between them; it is not proof of natural-language completeness.
"""
from copy import deepcopy
import json

from ..business.errors import BusinessToolRequestError


class WriteFieldConflict(BusinessToolRequestError):
    """Two model readings disagree; neither is an authoritative requirement."""

    def __init__(self, differences):
        self.differences = tuple(differences)
        super().__init__("requested write fields disagree between scope and proposal; review the original request")

    def projection(self):
        # Bounded field keys/phrases only; no values, targets or record contents.
        return [{"field_id": i, "part": part, "entity": entity, "field": field,
                 "in_scope": scoped, "in_proposal": proposed}
                for i, (part, entity, field, scoped, proposed) in enumerate(self.differences)]


def review_tool(conflict):
    return {"type": "function", "function": {"name": "review_skill_fields",
        "description": "Resolve EVERY disputed write field against the original REQUEST before proposing effects. Neither scope nor proposal is authoritative. An unsupported requested field remains requested.",
        "parameters": {"type": "object", "properties": {"decisions": {
            "type": "array", "minItems": len(conflict.differences), "maxItems": len(conflict.differences),
            "items": {"type": "object", "properties": {
                "field_id": {"type": "integer", "enum": list(range(len(conflict.differences)))},
                "requested": {"type": "boolean", "description": "Does the person ask to set/change this field? Tool support or its absence from the earlier scope is not a reason to say false."}},
                "required": ["field_id", "requested"], "additionalProperties": False}}},
            "required": ["decisions"], "additionalProperties": False}}}


def reviewed_requirements(conflict, args, parts):
    """Change only explicitly reviewed disagreements; retain all other obligations."""
    decisions = args.get("decisions") if isinstance(args, dict) else None
    if (not isinstance(args, dict) or set(args) != {"decisions"}
            or not isinstance(decisions, list) or len(decisions) != len(conflict.differences)
            or any(not isinstance(d, dict) or set(d) != {"field_id", "requested"}
                   or type(d["field_id"]) is not int or type(d["requested"]) is not bool
                   for d in decisions)
            or {d["field_id"] for d in decisions} != set(range(len(conflict.differences)))):
        raise BusinessToolRequestError("field review must decide every disagreement exactly once")
    result = deepcopy(parts)
    for decision in decisions:
        part, entity, field, _, _ = conflict.differences[decision["field_id"]]
        rows = result[part].setdefault("write_fields", [])
        if decision["requested"]:
            row = next((r for r in rows if r["entity"] == entity), None)
            if row is None:
                row = {"entity": entity, "fields": []}
                rows.append(row)
            if field not in row["fields"]:
                row["fields"].append(field)
        else:
            for row in rows:
                if row["entity"] == entity and field in row["fields"]:
                    row["fields"].remove(field)
            result[part]["write_fields"] = [r for r in rows if r["fields"]]
    return result


def field_catalog(index, definitions):
    entities = {entity for d in definitions for _, entity, _ in d.input_fields}
    rows = []
    for entity in index.entities:
        if entity.key in entities:
            rows.append({"entity": entity.key, "label": entity.label, "fields": [
                {"key": f.key, "label": f.label} for f in (*entity.fields, *entity.held)]})
    # A workflow may also have inputs on an entity without a list page. The
    # adapter's reviewed binding is vocabulary, not actor-visible record data.
    present = {r["entity"] for r in rows}
    for entity in sorted(entities - present):
        rows.append({"entity": entity, "fields": [{"key": key} for key in sorted({
            key for d in definitions for _, e, key in d.input_fields if e == entity})]})
    return json.dumps(rows, ensure_ascii=False, separators=(",", ":"))


def scope_tool(tool):
    tool = deepcopy(tool)
    tool["function"]["parameters"]["properties"]["write_requirements"] = {
        "type": "array", "maxItems": 24,
        "description": "Independently list EVERY requested write field, even when no installed operation supports it. Do not list target identifiers or automatically derived effects. Empty for read/explanation only.",
        "items": {"type": "object", "properties": {
            "part": {"type": "integer", "minimum": 0, "maximum": 7,
                "description": "Zero-based index of the corresponding handoff part."},
            "entity": {"type": "string"},
            "fields": {"type": "array", "minItems": 1, "maxItems": 40,
                "items": {"type": "string"}, "description":
                "All requested new/changed fields, using WRITE FIELD VOCABULARY keys. For an absent field keep the person's field phrase; do not omit it."}},
            "required": ["part", "entity", "fields"], "additionalProperties": False}}
    return tool


def bind_requirements(requirements, parts, *, selected):
    if not isinstance(requirements, list) or len(requirements) > 24:
        raise ValueError("write requirements must be a bounded array")
    for row in requirements:
        if (not isinstance(row, dict) or set(row) != {"part", "entity", "fields"}
                or type(row["part"]) is not int or not 0 <= row["part"] < len(parts)
                or parts[row["part"]]["kind"] != "handoff" or parts[row["part"]].get("rules")
                or not isinstance(row["entity"], str) or not 0 < len(row["entity"]) <= 80
                or not isinstance(row["fields"], list) or not 1 <= len(row["fields"]) <= 40
                or any(not isinstance(f, str) or not 0 < len(f) <= 80 for f in row["fields"])
                or len(set(row["fields"])) != len(row["fields"])):
            raise ValueError("invalid requested write field declaration")
        parts[row["part"]].setdefault("write_fields", []).append({
            "entity": row["entity"], "fields": list(row["fields"])})
    if selected and any(p["kind"] == "handoff" and not p.get("rules")
                        and not p.get("write_fields") for p in parts):
        raise ValueError("every selected Skill change requires its complete field requirements")


def _present(inputs, path):
    for key in path.split("."):
        if not isinstance(inputs, dict) or key not in inputs:
            return False
        inputs = inputs[key]
    return True


def require_coverage(plan, parts, definitions):
    definitions = {d.name: d for d in definitions}
    covered = {}
    for step in plan["steps"]:
        if step["kind"] != "operation":
            continue
        part = step.get("request_part")
        if (type(part) is not int or not 0 <= part < len(parts)
                or parts[part]["kind"] != "handoff" or parts[part].get("rules")):
            raise BusinessToolRequestError("each operation must bind its requested change part")
        for path, entity, field in definitions[step["operation"]].input_fields:
            if _present(step["inputs"], path):
                covered.setdefault(part, set()).add((entity, field))
    differences = []
    for position, part in enumerate(parts):
        required = {(r["entity"], f) for r in part.get("write_fields", []) for f in r["fields"]}
        proposed = covered.get(position, set())
        differences.extend((position, entity, field, (entity, field) in required,
                            (entity, field) in proposed)
                           for entity, field in sorted(required ^ proposed))
    if differences:
        raise WriteFieldConflict(differences)
