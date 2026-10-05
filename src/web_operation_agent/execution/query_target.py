"""Bind a declared text query to live controls, then to an application-verified read."""
from dataclasses import dataclass
import hashlib
import json
import re

from ..privacy import SENSITIVE_CREDENTIAL_NAME
from ..schemas import PageObservation
from ..schemas.browser import MAX_QUERY_KEY_CHARS, MAX_QUERY_VALUE_CHARS


@dataclass(frozen=True)
class QueryCondition:
    ref: str
    value: str
    label: str
    field: str
    route: str
    read_path: str

    @property
    def value_digest(self):
        encoded = json.dumps([self.value], ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()


def bind_query(raw, observation: PageObservation | None, maximum: int):
    if not isinstance(raw, list | tuple) or len(raw) > maximum:
        raise ValueError(f"query must list at most {maximum} control/value conditions")
    if not raw:
        return ()
    if (observation is None or observation.render_state != "ready"
            or not observation.page_route or observation.page_read is None):
        raise ValueError("query requires a ready rendered list and applied read")
    conditions = []
    for item in raw:
        if (not isinstance(item, dict) or set(item) != {"ref", "value"}
                or not isinstance(item["ref"], str) or not isinstance(item["value"], str)
                or not item["value"] or len(item["value"]) > MAX_QUERY_VALUE_CHARS):
            raise ValueError("each query needs one observed ref and bounded nonempty text value")
        elements = [e for e in observation.elements if e.ref == item["ref"]]
        if len(elements) != 1:
            raise ValueError("query control is not uniquely observed")
        element = elements[0]
        field = element.attrs.get("query_field")
        label = element.attrs.get("field_label")
        if (element.epoch != observation.epoch or element.role != "textbox"
                or element.attrs.get("page_control") != "query"
                or not element.attrs.get("form_instance") or not element.attrs.get("form_scope")
                or str(element.attrs["form_scope"]).startswith("dialog")
                or element.attrs.get("disabled") == "true" or element.attrs.get("readonly") == "true"
                or not isinstance(field, str) or len(field) > MAX_QUERY_KEY_CHARS
                or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", field) is None
                or SENSITIVE_CREDENTIAL_NAME.search(field)
                or not isinstance(label, str) or not label.strip()):
            raise ValueError("query control has no application-owned list field binding")
        if any(c.field == field for c in conditions):
            raise ValueError("query field is repeated")
        conditions.append(QueryCondition(item["ref"], item["value"], label, field,
                                        observation.page_route, observation.page_read["path"]))
    return tuple(conditions)


def query_unmet(conditions, observation, *, verified: bool):
    if not conditions:
        return ()
    if not verified or observation.page_read is None:
        return ("query result not application-verified",)
    proof = observation.page_read
    return tuple(f'query condition "{c.label}" not applied' for c in conditions
        if observation.page_route != c.route or proof["path"] != c.read_path
        or proof["query_fields"].get(c.field) != c.value_digest)
