"""The bounded readonly subset of the existing goal declaration vocabulary."""
from .spec import FILTER_OPS


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

DATA_READ_SCHEMAS = (
    {"type": "function", "function": {"name": "inspect_data", "description":
        "Discover the acting user's data entities, field labels and readonly page scopes. "
        "A bounded ranked sample; metadata is not data or proof of absence. Supply entity to focus.",
        "parameters": _object({"query": {"type": "string", "minLength": 1, "maxLength": 500},
                               "entity": _text}, ("query",))}},
    {"type": "function", "function": {"name": "resolve_data", "description":
        "Read facts, scoped counts/sums, numeric min/max (all ties), or lists through the "
        "application's deterministic resolver. Use entity keys and actual indexed field labels; "
        "Prefer inspect_data then this tool for business answers, including linked names, "
        "full-scope aggregates and extrema, rather than rebuilding those reads with opaque IDs. "
        "copy record references and conditions from the user. A page key scopes data; it does "
        "not navigate. Every result returns to this same Agent with typed evidence and failures. "
        "No writes, source rules, form entry or page operations. Mixed tasks keep all raw requirements.",
        "parameters": _object({"deliverables": {"type": "array", "minItems": 1, "maxItems": 4,
                                                "items": _deliverable}}, ("deliverables",))}},
)
