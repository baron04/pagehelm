"""The bounded readonly subset of the existing goal declaration vocabulary."""
from .spec import FILTER_OPS, DECLARE_GOAL_TOOL
from ..business.entities import (DISCOVER_ENTITIES_TOOL, DESCRIBE_ENTITY_TOOL,
                                QUERY_ENTITY_RECORDS_TOOL)


def _object(properties, required=()):
    return {"type": "object", "additionalProperties": False, "properties": properties,
            "required": list(required)}


_text = {"type": "string", "maxLength": 120}
_strings = {"type": "array", "maxItems": 12, "items": _text}
_filter = _object({"field": _text, "values": {"type": "array", "maxItems": 6, "items": _text},
                  "op": {"type": "string", "enum": list(FILTER_OPS)}}, ("field", "values"))
_metric = _object({"op": {"type": "string", "enum": ["count", "sum", "min", "max"]},
    "field": _text, "filters": {"type": "array", "maxItems": 6, "items": _filter},
    "page_filters": {**_strings, "maxItems": 8}, "group_by": _text,
    "top": {"type": "integer", "minimum": 0, "maximum": 20}, "total": {"type": "boolean"}}, ("op",))
_deliverable = _object({"kind": {"type": "string", "enum": ["fact", "list", "aggregate"]},
    "entity": _text, "record": _text, "via": _text, "fields": _strings, "metric": _metric,
    "page": _object({"key": _text, "search": _text}), "sort": _text,
    "descending": {"type": "boolean"}, "limit": {"type": "integer", "minimum": 0, "maximum": 50}},
    ("kind", "entity"))

# Preserve the established declaration semantics when exposing its readonly subset.
# Bounds/types are this tool's; descriptions have one owner in the goal vocabulary.
_goal_properties = DECLARE_GOAL_TOOL["function"]["parameters"]["properties"]["deliverables"]["items"]["properties"]
for _key in ("entity", "record", "via", "fields", "sort", "descending", "limit"):
    if "description" in _goal_properties[_key]:
        _deliverable["properties"][_key]["description"] = _goal_properties[_key]["description"]
for _key in _metric["properties"]:
    if "description" in _goal_properties["metric"]["properties"][_key]:
        _metric["properties"][_key]["description"] = _goal_properties["metric"]["properties"][_key]["description"]
_metric["description"] = ("Conditions for facts and lists also belong here in filters. "
    "Numeric highest/lowest means min/max and ALL ties, not a sorted top-N list. "
    "A list with op=count retains the rows and applies these conditions. "
    "For a requested overall sum set total=true. Select the requested business measure, "
    "not a neighbouring tax or discount field.")

DATA_READ_REPLACED_TOOLS = frozenset({DISCOVER_ENTITIES_TOOL, DESCRIBE_ENTITY_TOOL,
                                     QUERY_ENTITY_RECORDS_TOOL})

DATA_READ_SCHEMAS = (
    {"type": "function", "function": {"name": "inspect_data", "description":
        "Discover the acting user's data entities, field labels and readonly page scopes. "
        "A bounded ranked sample; metadata is not data or proof of absence. Supply entity to focus.",
        "parameters": _object({"query": {"type": "string", "minLength": 1, "maxLength": 500},
                               "entity": _text}, ("query",))}},
    {"type": "function", "function": {"name": "resolve_data", "description":
        "Read facts, scoped counts/sums, numeric min/max (all ties), or lists through the "
        "application's deterministic resolver. Use entity keys and actual indexed field labels; "
        "Use inspect_data before this tool to see actual field labels/aliases, including linked names, "
        "full-scope aggregates and extrema, rather than rebuilding those reads with opaque IDs. "
        "copy record references and conditions from the user. Owner/date/status restrictions "
        "belong in metric.filters even for lists. An unrequested broader scope is not an answer. "
        "Use native aggregate evidence or compute; writing arithmetic in a statement cannot "
        "deliver a calculated value. A page key scopes data; it does "
        "not navigate. Every result returns to this same Agent with typed evidence and failures. "
        "No writes, source rules, form entry or page operations. Mixed tasks keep all raw requirements.",
        "parameters": _object({"deliverables": {"type": "array", "minItems": 1, "maxItems": 4,
                                                "items": _deliverable}}, ("deliverables",))}},
)

# An opt-in model surface; the established declaration schema remains the default.
# Each call asks for one operation, while the server owns the GoalSpec compilation.
SIMPLE_DATA_READ_SCHEMAS = (DATA_READ_SCHEMAS[0], {
    "type": "function", "function": {"name": "read_data", "description":
        "Read one actor-scoped operation using exact entities and field labels from inspect_data. "
        "record reads fields of a named record, or one uniquely selected by filters. "
        "list returns bounded rows; count counts every matching row; sum totals the field over "
        "every matching row; min/max compare the complete scope and retain ALL tied records. "
        "Put every owner/date/status restriction in filters, including for records and lists. "
        "A relationship scope is a filter on the indexed link field with the exact related "
        "record name. via follows an indexed link of a named record to read that target's fields. "
        "page is an optional indexed query scope and never navigates. search is an explicit "
        "record-name substring for lists. Missing or unusable conditions are refused; do not "
        "substitute neighbouring fields or broaden the user's scope. sum needs field, never "
        "a nested metric or total flag. No writes, source rules or page operations. "
        "Results and typed evidence return to this same Agent before its next decision.",
        "parameters": _object({
            "operation": {"type": "string", "enum": ["record", "list", "count", "sum", "min", "max"]},
            "entity": _text, "record": _text, "via": _text,
            "fields": {**_strings, "description": "Requested columns for record/list, or all min/max ties."},
            "field": {**_text, "description": "Numeric field required for sum/min/max."},
            "filters": {"type": "array", "maxItems": 6, "items": _filter},
            "page": {**_text, "description": "Exact indexed page key selecting the read scope."},
            "page_filters": {**_strings, "maxItems": 8},
            "search": {**_text, "description": "Explicit record-name substring; list only."},
            "group_by": {**_text, "description": "Exact indexed field; count only. Groups may be bounded."},
            "sort": {**_text, "description": "Exact indexed field; list only."},
            "descending": {"type": "boolean"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50,
                      "description": "Maximum listed rows; list only. Does not limit aggregates."},
        }, ("operation", "entity"))}})
