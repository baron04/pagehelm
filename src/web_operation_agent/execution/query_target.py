"""Bind a declared query to live controls and an application-verified read."""
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
    choice: bool = False
    scope: str = ""
    instance: str = ""

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
        label = element.attrs.get("field_label", element.name)
        scope, instance = element.attrs.get("form_scope"), element.attrs.get("form_instance")
        if (element.epoch != observation.epoch or element.role not in {"textbox", "searchbox", "combobox"}
                or element.attrs.get("page_control") != "query"
                or (scope is not None or instance is not None) and (not scope or not instance)
                or str(scope or "").startswith("dialog")
                or element.attrs.get("disabled") == "true" or element.attrs.get("readonly") == "true"
                or not isinstance(field, str) or len(field) > MAX_QUERY_KEY_CHARS
                or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", field) is None
                or SENSITIVE_CREDENTIAL_NAME.search(field)
                or not isinstance(label, str) or not label.strip()):
            raise ValueError("query control has no application-owned list field binding")
        if element.role == "combobox" and (not scope or not instance
                or element.attrs.get("aria-multiselectable") == "true"):
            raise ValueError("query choice requires one bound single-choice control")
        if any(c.field == field for c in conditions):
            raise ValueError("query field is repeated")
        conditions.append(QueryCondition(item["ref"], item["value"], label, field,
            observation.page_route, observation.page_read["path"],
            element.role == "combobox", str(scope or ""), str(instance or "")))
    return tuple(conditions)


def query_unmet(conditions, observation, *, verified: bool):
    if not conditions:
        return ()
    if not verified or observation.page_read is None:
        return ("query result not application-verified",)
    proof = observation.page_read
    def matches(condition):
        if not condition.choice:
            return proof["query_fields"].get(condition.field) == condition.value_digest
        # Application verification binds this selection to the successful read, and
        # ActionAdmission stamps the same query controls. A draft label alone proves nothing.
        controls = [e for e in observation.elements if e.role == "combobox"
            and e.attrs.get("page_control") == "query"
            and e.attrs.get("query_field") == condition.field
            and e.attrs.get("form_scope") == condition.scope
            and e.attrs.get("form_instance") == condition.instance]
        return (len(controls) == 1 and controls[0].epoch == observation.epoch
            and controls[0].attrs.get("selected_option") == condition.value
            and condition.field in proof["query_fields"])
    return tuple(f'query condition "{c.label}" not applied' for c in conditions
        if observation.page_route != c.route or proof["path"] != c.read_path or not matches(c))
