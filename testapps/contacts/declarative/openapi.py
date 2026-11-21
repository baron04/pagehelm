"""Declarative application specs, OpenAPI 3.x importer, and frontend presets.

Hosts declare their application using either an OpenAPI 3.x document parsed through
:func:`entities_from_openapi` or explicit :class:`EntitySpec`, :class:`PageSpec`, and
:class:`FormSpec` descriptors instead of hand-writing boilerplate read/write ports.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping

from web_operation_agent.business.forms import FormChoice
from web_operation_agent.business.write_pipeline import DEFAULT_SECRET_FIELD_PATTERN


@dataclass(frozen=True, slots=True)
class FrontendPreset:
    """Metadata describing a supported browser UI integration preset."""

    name: str
    navigation_mode: str
    helpers_dir: str = ""
    description: str = ""


FRONTEND_PRESETS: dict[str, FrontendPreset] = {
    "element-plus": FrontendPreset(
        name="element-plus",
        navigation_mode="spa",
        helpers_dir="frontend/src/integrations/element-plus",
        description="Vue 3 + Element Plus shared helpers (table, select, date-picker, form, vue).",
    ),
    "plain-html": FrontendPreset(
        name="plain-html",
        navigation_mode="spa",
        helpers_dir="",
        description="Standard semantic HTML/DOM PageController without a component-library layer.",
    ),
}


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """Unified field declaration for reads, summaries, forms, and symmetric updates."""

    name: str
    label: str = ""
    kind: str = "text"
    required: bool = False
    searchable: bool = True
    summable: bool = False
    fixed_unit: str = ""
    max_length: int | None = None
    min_length: int | None = None
    minimum: float | None = None
    maximum: float | None = None
    aliases: tuple[str, ...] = ()
    search_text: str = ""
    unit_scale: int = 1
    choices: tuple[FormChoice, ...] = ()
    controls: tuple[str, ...] = ("textbox",)
    binding: str = "field"
    updateable: bool = True
    in_form: bool = True
    in_entity: bool = True
    form_kind: str = ""
    summary_label: str = ""
    summary_kind: str = ""
    normalizer: Callable[[Any], Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("FieldSpec.name must be a non-empty string")
        if not self.label:
            object.__setattr__(self, "label", self.name.replace("_", " ").title())
        if not self.summary_label:
            object.__setattr__(self, "summary_label", self.label)
        if not self.summary_kind:
            kind_map = {
                "integer": "number",
                "number": "number",
                "monetary": "number",
                "boolean": "boolean",
                "date": "date",
                "datetime": "datetime",
                "reference": "relation",
            }
            object.__setattr__(self, "summary_kind", kind_map.get(self.kind, "text"))
        if not self.form_kind:
            form_map = {
                "text": "text",
                "integer": "integer",
                "number": "number",
                "monetary": "number",
                "date": "date",
                "datetime": "datetime",
                "choice": "choice",
            }
            object.__setattr__(self, "form_kind", form_map.get(self.kind, "text"))

    @property
    def is_secret(self) -> bool:
        return bool(
            DEFAULT_SECRET_FIELD_PATTERN.search(self.name)
            or (self.label and DEFAULT_SECRET_FIELD_PATTERN.search(self.label))
        )


@dataclass(frozen=True, slots=True)
class EntitySpec:
    """Declarative business entity specification."""

    name: str
    label: str
    fields: tuple[FieldSpec, ...]
    singular_label: str = ""
    id_field: str = "id"
    name_field: str = "name"
    detail_field: str = ""
    subject_reference: str = ""
    aliases: tuple[str, ...] = ()
    search_text: str = ""
    list_route: str = ""
    detail_route_template: str = ""
    create_route: str = ""
    list_endpoint: str = ""
    detail_endpoint_template: str = ""
    update_endpoint_template: str = ""
    collection_key: str = ""
    detail_key: str = ""
    read_scope: str = ""
    write_scope: str = ""
    list_tool_name: str = ""
    list_tool_description: str = ""
    list_filter_param: str = ""
    list_filter_field: str = ""
    list_filter_description: str = ""
    convention_update: bool = False
    volatile_fields: frozenset[str] = frozenset()
    summary_source_label: str = ""
    page_views: tuple[str, ...] = ("list", "detail")
    page_title: str = ""
    row_fetcher: Callable[..., list[dict]] | None = None
    record_reader: Callable[..., dict | None] | None = None
    update_dispatcher: Callable[..., None] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("EntitySpec.name must be non-empty")
        if not isinstance(self.label, str) or not self.label.strip():
            raise ValueError("EntitySpec.label must be non-empty")
        if not self.fields:
            raise ValueError(f"EntitySpec {self.name!r} requires at least one FieldSpec")
        if not self.subject_reference:
            object.__setattr__(self, "subject_reference", self.name)
        if not self.page_title:
            object.__setattr__(self, "page_title", self.label)
        if not self.search_text:
            tokens = [self.name, self.label, *self.aliases]
            object.__setattr__(self, "search_text", " ".join(dict.fromkeys(t for t in tokens if t)))

    def field_by_name(self, name: str) -> FieldSpec | None:
        return next((f for f in self.fields if f.name == name), None)


@dataclass(frozen=True, slots=True)
class QueryFormSpec:
    """Reviewed page-level filter/search form specification."""

    route: str
    form_scope: str
    field_name: str
    field_label: str
    field_attr: str = "query_field"
    submit_label: str = ""
    api_path: str = ""


@dataclass(frozen=True, slots=True)
class PageSpec:
    """Declarative reviewed UI page specification."""

    key: str
    label: str
    route: str
    entity: str = ""
    subject_reference: str = ""
    views: tuple[str, ...] = ()
    sections: tuple[str, ...] = ()
    section_tab_ids: tuple[str, ...] = ()
    operations: tuple[str, ...] = ()
    search_text: str = ""
    breadcrumbs: tuple[str, ...] = ()
    query_form: QueryFormSpec | None = None
    always_visible: bool = False

    def __post_init__(self) -> None:
        if not self.breadcrumbs:
            object.__setattr__(self, "breadcrumbs", (self.label,))
        if not self.search_text:
            object.__setattr__(self, "search_text", self.label)


@dataclass(frozen=True, slots=True)
class FormSpec:
    """Declarative transient form entry & prefill specification."""

    reference: str
    title: str
    route: str
    control_scope: str
    submit_label: str
    fields: tuple[FieldSpec, ...]
    scope_description: str = ""
    required_scope: str = ""
    entry_route: str = ""
    entry_label: str = ""
    entry_reference: str = ""
    entry_action: str = "create"
    draft_routes: tuple[str, ...] = ()
    edit_control_scope: str = ""
    local_line_prefix: str = ""
    add_line_button: str = ""
    prefill_from_tool: str = ""
    normalizers: Mapping[str, Callable[[Any], Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.scope_description:
            object.__setattr__(
                self, "scope_description", f"fields writable by the current actor on {self.route}"
            )
        if not self.draft_routes:
            object.__setattr__(self, "draft_routes", (self.route,))


def _resolve_ref(doc: Mapping[str, Any], schema: Any, seen: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(schema, Mapping):
        return {}
    ref = schema.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return dict(schema)
    seen = seen or set()
    if ref in seen:
        return {}
    seen.add(ref)
    node: Any = doc
    for part in ref[2:].split("/"):
        if isinstance(node, Mapping) and part in node:
            node = node[part]
        else:
            return {}
    return _resolve_ref(doc, node, seen)


def _json_response_schema(doc: Mapping[str, Any], operation: Mapping[str, Any]) -> dict[str, Any]:
    responses = operation.get("responses")
    if not isinstance(responses, Mapping):
        return {}
    for code in ("200", "201", 200, 201):
        resp = responses.get(code)
        content = resp.get("content") if isinstance(resp, Mapping) else None
        app_json = content.get("application/json") if isinstance(content, Mapping) else None
        if isinstance(app_json, Mapping) and "schema" in app_json:
            return _resolve_ref(doc, app_json["schema"])
    return {}


def _field_from_openapi(name: str, raw_prop: Mapping[str, Any], *, required: bool, id_field: str) -> FieldSpec:
    oa_type = str(raw_prop.get("type") or "string")
    oa_format = str(raw_prop.get("format") or "")
    title = str(raw_prop.get("title") or name.replace("_", " ").title())
    choices: tuple[FormChoice, ...] = ()
    enum_vals = raw_prop.get("enum")
    if isinstance(enum_vals, list) and enum_vals:
        kind, controls = "choice", ("combobox",)
        choices = tuple(FormChoice(str(v), str(v).title()) for v in enum_vals)
    elif oa_type == "integer":
        kind, controls = "integer", ("spinbutton",)
    elif oa_type == "number":
        kind, controls = "number", ("textbox",)
    elif oa_type == "boolean":
        kind, controls = "boolean", ("checkbox",)
    elif oa_format in {"date", "date-time"}:
        kind, controls = ("date" if oa_format == "date" else "datetime"), ("textbox",)
    else:
        kind, controls = "text", ("textbox",)
    secret = bool(DEFAULT_SECRET_FIELD_PATTERN.search(name) or DEFAULT_SECRET_FIELD_PATTERN.search(title))
    return FieldSpec(
        name=name, label=title, kind=kind, form_kind=("email" if oa_format == "email" else ""),
        required=required,
        max_length=raw_prop.get("maxLength") if isinstance(raw_prop.get("maxLength"), int) else None,
        min_length=raw_prop.get("minLength") if isinstance(raw_prop.get("minLength"), int) else None,
        minimum=raw_prop.get("minimum") if isinstance(raw_prop.get("minimum"), (int, float)) else None,
        maximum=raw_prop.get("maximum") if isinstance(raw_prop.get("maximum"), (int, float)) else None,
        choices=choices, controls=controls, updateable=(name != id_field and not secret),
    )


def entities_from_openapi(
    document: Mapping[str, Any],
    *,
    entity_overrides: Mapping[str, Mapping[str, Any]] | None = None,
    default_read_scope: str = "",
    default_write_scope: str = "",
) -> tuple[EntitySpec, ...]:
    """Build :class:`EntitySpec` declarations from an OpenAPI 3.x document."""
    paths = document.get("paths")
    if not isinstance(paths, Mapping):
        raise ValueError("OpenAPI document must contain a 'paths' mapping")
    overrides = dict(entity_overrides or {})
    entities: list[EntitySpec] = []

    for path, path_item in paths.items():
        if not isinstance(path, str) or "{" in path or not isinstance(path_item, Mapping):
            continue
        get_op = path_item.get("get")
        if not isinstance(get_op, Mapping):
            continue
        resp_schema = _json_response_schema(document, get_op)
        collection_key, item_schema = "", {}
        if resp_schema.get("type") == "array":
            item_schema = _resolve_ref(document, resp_schema.get("items"))
        elif resp_schema.get("type") == "object":
            for prop_name, prop_schema in (resp_schema.get("properties") or {}).items():
                resolved_prop = _resolve_ref(document, prop_schema)
                if resolved_prop.get("type") == "array":
                    collection_key = str(prop_name)
                    item_schema = _resolve_ref(document, resolved_prop.get("items"))
                    break
        if item_schema.get("type") != "object" or not isinstance(item_schema.get("properties"), Mapping):
            continue

        segment = path.rstrip("/").rsplit("/", 1)[-1]
        entity_name = str(get_op.get("x-woa-entity") or segment)
        ov = dict(overrides.get(entity_name, {}))
        id_field = str(ov.get("id_field") or item_schema.get("x-woa-id-field") or "id")
        required_names = set(item_schema.get("required") or ())
        parsed_fields = [
            fspec
            for fname, fschema in item_schema["properties"].items()
            for fspec in (
                _field_from_openapi(
                    str(fname), _resolve_ref(document, fschema),
                    required=str(fname) in required_names, id_field=id_field,
                ),
            )
            if not fspec.is_secret
        ]
        if not parsed_fields:
            continue

        detail_template, update_template, detail_key, has_put = "", "", "", False
        prefix = path.rstrip("/") + "/{"
        for sub_path, sub_item in paths.items():
            if not isinstance(sub_path, str) or not isinstance(sub_item, Mapping):
                continue
            if sub_path.startswith(prefix) and sub_path.endswith("}") and "/" not in sub_path[len(prefix) :]:
                if isinstance(sub_item.get("get"), Mapping):
                    detail_template = sub_path
                    dschema = _json_response_schema(document, sub_item["get"])
                    dprops = dschema.get("properties") if dschema.get("type") == "object" else None
                    if isinstance(dprops, Mapping) and set(dprops) != set(item_schema["properties"]) and len(dprops) == 1:
                        detail_key = next(iter(dprops))
                if isinstance(sub_item.get("put"), Mapping):
                    update_template, has_put = sub_path, True
                break

        label = str(ov.get("label") or item_schema.get("title") or entity_name.replace("_", " ").replace("-", " ").title())
        spec_kwargs: dict[str, Any] = {
            "name": entity_name, "label": label, "fields": tuple(parsed_fields), "id_field": id_field,
            "list_endpoint": path, "detail_endpoint_template": detail_template,
            "update_endpoint_template": update_template, "collection_key": collection_key,
            "detail_key": detail_key, "read_scope": default_read_scope, "write_scope": default_write_scope,
            "convention_update": has_put, "list_route": "/" + entity_name.lstrip("/"),
            "detail_route_template": "/" + entity_name.lstrip("/") + "/{id}",
        }
        field_overrides = ov.pop("field_overrides", None)
        if isinstance(field_overrides, Mapping):
            spec_kwargs["fields"] = tuple(
                replace(f, **field_overrides[f.name]) if f.name in field_overrides else f
                for f in spec_kwargs["fields"]
            )
        extra_fields = ov.pop("extra_fields", ())
        if extra_fields:
            spec_kwargs["fields"] = (*spec_kwargs["fields"], *extra_fields)
        spec_kwargs.update(ov)
        entities.append(EntitySpec(**spec_kwargs))

    return tuple(entities)


__all__ = [
    "EntitySpec", "FRONTEND_PRESETS", "FieldSpec", "FormChoice",
    "FormSpec", "FrontendPreset", "PageSpec", "QueryFormSpec", "entities_from_openapi",
]
