"""Application-neutral discovery and preparation of the form currently on screen.

The page says which controls exist now; an application adapter says which semantic fields
the current actor may fill and how their values are validated.  Neither source is enough on
its own.  :class:`ApplicationFormTools` intersects them and gives the model only opaque,
one-observation bindings.  The resulting :class:`FormDraft` is still applied by the existing
generic browser handoff, which never submits the form.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from .errors import (BusinessToolContractError, BusinessToolIdentityError,
                     BusinessToolPermissionError, BusinessToolRequestError)
from .tools.contracts import (APPLICATION_SAVES_ON_LEAVE, FORM_PERSISTENCE,
                              FormDraft, FormDraftField)
from ..schemas import PageObservation
from ..state.identity import OperationContext, PrincipalError


DESCRIBE_FORM_TOOL = "describe_form"
PREPARE_FORM_TOOL = "prepare_form"
FORM_TOOLS = frozenset({DESCRIBE_FORM_TOOL, PREPARE_FORM_TOOL})
_FIELD_REFERENCE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,79}$")
_OPAQUE_FORM = re.compile(r"^form_[0-9a-f]{32}$")
_OPAQUE_FIELD = re.compile(r"^form_field_[0-9a-f]{32}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SENSITIVE = re.compile(
    r"(?:password|passwd|pwd|secret|token|api[_-]?key|authorization)", re.I)
_KINDS = frozenset({"text", "email", "phone", "url", "integer", "number",
                    "date", "datetime", "choice"})
_CONTROLS = frozenset({"textbox", "combobox", "select"})


DESCRIBE_FORM_SCHEMA = {
    "type": "function",
    "function": {
        "name": DESCRIBE_FORM_TOOL,
        "description": (
            "Describe fields that both the current page exposes and the application allows "
            "this actor to fill. Returns short-lived opaque form and field IDs, never DOM "
            "selectors. Use source lookup separately when static rules need explanation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "field_query": {
                    "type": "string", "maxLength": 200,
                    "description": "Optional words naming the fields needed for this task.",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "additionalProperties": False,
        },
    },
}

PREPARE_FORM_SCHEMA = {
    "type": "function",
    "function": {
        "name": PREPARE_FORM_TOOL,
        "description": (
            "Validate user-supplied or source-derived values against a fresh application "
            "descriptor and the unchanged current page, then create a one-use draft. "
            "For a source-derived value include source_basis with a source#N reference "
            "and exact support span from this turn. Use only IDs returned by describe_form. "
            "This does not type, submit, or persist anything."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "form_id": {"type": "string", "pattern": "^form_[0-9a-f]{32}$"},
                "values": {
                    "type": "array", "minItems": 1, "maxItems": 20,
                    "items": {
                        "type": "object",
                        "properties": {
                            "field_id": {
                                "type": "string",
                                "pattern": "^form_field_[0-9a-f]{32}$",
                            },
                            "value": {"type": "string", "maxLength": 4000},
                            "source_basis": {
                                "type": "object",
                                "properties": {
                                    "ref": {"type": "string", "pattern": "^source#[1-9][0-9]*$"},
                                    "support": {"type": "string", "minLength": 8,
                                                "maxLength": 400},
                                },
                                "required": ["ref", "support"],
                                "additionalProperties": False,
                            },
                        },
                        "required": ["field_id", "value"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["form_id", "values"],
            "additionalProperties": False,
        },
    },
}

FORM_TOOL_SCHEMAS = (DESCRIBE_FORM_SCHEMA, PREPARE_FORM_SCHEMA)


def _text(value: object, name: str, maximum: int, *, empty: bool = False) -> str:
    if (not isinstance(value, str) or len(value) > maximum or _CONTROL.search(value)
            or (not empty and not value.strip())):
        raise ValueError(f"{name} is invalid")
    return value.strip() if not empty else value


@dataclass(frozen=True, slots=True)
class FormChoice:
    value: str
    label: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _text(
            self.value, "form choice value", 500, empty=True))
        object.__setattr__(self, "label", _text(self.label, "form choice label", 120))


@dataclass(frozen=True, slots=True)
class ApplicationFormField:
    """One adapter-owned writable field; its native reference never reaches the model."""

    reference: str
    label: str
    kind: str = "text"
    controls: tuple[str, ...] = ("textbox",)
    required: bool = False
    choices: tuple[FormChoice, ...] = ()

    def __post_init__(self) -> None:
        if (not isinstance(self.reference, str)
                or not _FIELD_REFERENCE.fullmatch(self.reference)
                or _SENSITIVE.search(self.reference)):
            raise ValueError("application form field reference is invalid")
        object.__setattr__(self, "label", _text(self.label, "form field label", 120))
        if self.kind not in _KINDS:
            raise ValueError("application form field kind is invalid")
        if (not isinstance(self.controls, tuple) or not self.controls
                or len(self.controls) != len(set(self.controls))
                or any(item not in _CONTROLS for item in self.controls)):
            raise ValueError("application form field controls are invalid")
        if not isinstance(self.required, bool):
            raise ValueError("application form field required must be boolean")
        if (not isinstance(self.choices, tuple)
                or any(not isinstance(item, FormChoice) for item in self.choices)
                or len({item.value for item in self.choices}) != len(self.choices)
                or len(self.choices) > 200):
            raise ValueError("application form field choices are invalid")
        if self.kind == "choice" and not self.choices:
            raise ValueError("choice form fields require declared choices")
        if self.kind != "choice" and self.choices:
            raise ValueError("only choice form fields may declare choices")


@dataclass(frozen=True, slots=True)
class ApplicationFormDescriptor:
    """The adapter's current, actor-scoped meaning for one form route."""

    reference: str
    title: str
    route: str
    fields: tuple[ApplicationFormField, ...]
    scope: str
    persistence: str = "transient_until_submit"
    effect: str = ""
    targets: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "reference", _text(
            self.reference, "application form reference", 300))
        object.__setattr__(self, "title", _text(self.title, "application form title", 200))
        object.__setattr__(self, "scope", _text(self.scope, "application form scope", 300))
        parsed = urlsplit(self.route) if isinstance(self.route, str) else None
        if not (parsed and self.route.startswith("/") and not self.route.startswith("//")
                and not parsed.scheme and not parsed.netloc and not parsed.fragment
                and len(self.route) <= 1000 and "%" not in parsed.path
                and not re.search(r"[\\\x00-\x20\x7f]", self.route)
                and ".." not in parsed.path.split("/")):
            raise ValueError("application form route is invalid")
        if (not isinstance(self.fields, tuple) or not 1 <= len(self.fields) <= 2000
                or any(not isinstance(item, ApplicationFormField) for item in self.fields)
                or len({item.reference for item in self.fields}) != len(self.fields)):
            raise ValueError("application form fields are invalid or repeated")
        if not isinstance(self.persistence, str) or self.persistence not in FORM_PERSISTENCE:
            raise ValueError("application form persistence is invalid")
        if not isinstance(self.effect, str) or not isinstance(self.targets, tuple):
            raise ValueError("application form effect and targets are invalid")
        if self.persistence == APPLICATION_SAVES_ON_LEAVE:
            object.__setattr__(self, "effect", _text(
                self.effect, "application form effect", 300))
            if (not isinstance(self.targets, tuple) or not 1 <= len(self.targets) <= 24
                    or any(not isinstance(item, str) or not item.strip()
                           or len(item) > 300 for item in self.targets)):
                raise ValueError("application form targets are invalid")
        elif self.effect or self.targets:
            raise ValueError("a transient application form states no effect or targets")


@dataclass(frozen=True, slots=True)
class ApplicationFormValue:
    field: ApplicationFormField
    value: str
    control: str

    def __post_init__(self) -> None:
        if not isinstance(self.field, ApplicationFormField):
            raise ValueError("application form value field is invalid")
        if self.control not in self.field.controls:
            raise ValueError("application form value control is invalid")
        _text(self.value, "application form value", 4000, empty=True)


@runtime_checkable
class ApplicationFormPort(Protocol):
    """Application translation boundary for current-form metadata and validation."""

    @property
    def required_scopes(self) -> frozenset[str]: ...

    def describe_form(self, observation: PageObservation, *,
                      context: OperationContext | None = None
                      ) -> ApplicationFormDescriptor | None: ...

    def prepare_form(self, form: ApplicationFormDescriptor,
                     values: tuple[ApplicationFormValue, ...], *,
                     context: OperationContext | None = None) -> FormDraft: ...


@dataclass(frozen=True, slots=True)
class NoApplicationForms:
    """Default port: this integration installs no generic form capability."""

    required_scopes: frozenset[str] = frozenset()

    def describe_form(self, observation: PageObservation, *,
                      context: OperationContext | None = None
                      ) -> ApplicationFormDescriptor | None:
        return None

    def prepare_form(self, form: ApplicationFormDescriptor,
                     values: tuple[ApplicationFormValue, ...], *,
                     context: OperationContext | None = None) -> FormDraft:
        raise BusinessToolRequestError("this application exposes no generic form")


@dataclass(frozen=True, slots=True)
class _VisibleField:
    descriptor: ApplicationFormField
    control: str


class ApplicationFormTools:
    """Create and revalidate opaque bindings for the current page's writable fields."""

    def __init__(self, port: ApplicationFormPort, *, application_id: str,
                 binding_secret: bytes | None = None):
        if not isinstance(port, ApplicationFormPort):
            raise TypeError("application form port does not implement the required contract")
        if not isinstance(application_id, str) or not application_id.strip():
            raise ValueError("application form tools need an application_id")
        secret = binding_secret if binding_secret is not None else secrets.token_bytes(32)
        if not isinstance(secret, bytes) or len(secret) < 16:
            raise ValueError("application form binding secret is too short")
        self._port = port
        self._application_id = application_id
        self._secret = secret
        scopes = port.required_scopes
        if (not isinstance(scopes, frozenset)
                or any(not isinstance(item, str)
                       or not re.fullmatch(r"[a-z][a-z0-9:._-]{1,99}", item)
                       for item in scopes)):
            raise TypeError("application form required_scopes are invalid")
        self._required_scopes = scopes

    @staticmethod
    def tool_schemas() -> tuple[dict, ...]:
        return FORM_TOOL_SCHEMAS

    def _descriptor(self, observation: PageObservation,
                    context: OperationContext | None) -> ApplicationFormDescriptor | None:
        if context is None and self._required_scopes:
            raise BusinessToolIdentityError(
                "generic forms require a verified application identity")
        if context is not None:
            try:
                context.require_live(application_id=self._application_id)
            except PrincipalError as exc:
                raise BusinessToolIdentityError(
                    "the application identity is invalid or expired") from exc
            missing = sorted(self._required_scopes - set(context.principal.scopes))
            if missing:
                raise BusinessToolPermissionError(
                    "the current actor was not granted generic form preparation",
                    layer="tool_authorization", required_scope=missing[0])
        try:
            form = self._port.describe_form(observation, context=context)
        except (BusinessToolContractError, BusinessToolRequestError):
            raise
        except (TypeError, ValueError) as exc:
            raise BusinessToolContractError(
                "the application returned an invalid form descriptor") from exc
        if form is not None and not isinstance(form, ApplicationFormDescriptor):
            raise BusinessToolContractError(
                "the application returned an invalid form descriptor")
        if form is not None and not self._at_route(observation, form.route):
            raise BusinessToolContractError(
                "the application described a form other than the current page")
        return form

    @staticmethod
    def _at_route(observation: PageObservation, route: str) -> bool:
        actual, expected = urlsplit(observation.url), urlsplit(route)
        return actual.path == expected.path and actual.query == expected.query

    @staticmethod
    def _metadata(form: ApplicationFormDescriptor) -> dict:
        return {
            "reference": form.reference, "title": form.title, "route": form.route,
            "scope": form.scope, "persistence": form.persistence,
            "effect": form.effect, "targets": list(form.targets),
            "fields": [{
                "reference": field.reference, "label": field.label, "kind": field.kind,
                "controls": list(field.controls), "required": field.required,
                "choices": [{"value": item.value, "label": item.label}
                            for item in field.choices],
            } for field in form.fields],
        }

    def _binding(self, form: ApplicationFormDescriptor, observation: PageObservation,
                 context: OperationContext | None) -> str:
        payload = {
            "application": self._application_id,
            "form": self._metadata(form),
            "page": {"url": observation.url, "epoch": observation.epoch,
                     "digest": observation.content_digest},
            "actor": (context.principal.actor_authority_digest if context is not None else ""),
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)

    def _opaque(self, prefix: str, *parts: str) -> str:
        digest = hmac.new(self._secret, "\0".join(parts).encode("utf-8"),
                          hashlib.sha256).hexdigest()[:32]
        return prefix + digest

    def _visible(self, form: ApplicationFormDescriptor,
                 observation: PageObservation) -> tuple[_VisibleField, ...]:
        visible = []
        for field in form.fields:
            matches = [element for element in observation.elements
                       if element.attrs.get("field") == field.reference
                       and element.role in field.controls
                       and not self._unavailable(element.attrs)]
            if len(matches) == 1:
                visible.append(_VisibleField(field, matches[0].role))
        # The shared browser contract already caps a whole observation at 200 controls.
        # Keep every supported control here so a field_query can find a lower-page field;
        # the Agent-facing result is independently capped at twenty below.
        return tuple(visible)

    @staticmethod
    def _unavailable(attrs: dict) -> bool:
        """Whether current observation facts say a control cannot be written."""
        for name in ("disabled", "readonly", "read-only"):
            if name not in attrs:
                continue
            value = attrs[name]
            if value is True or (isinstance(value, str)
                                 and value.casefold() not in {"", "0", "false", "no", "off"}):
                return True
        return False

    @staticmethod
    def _validate_value(field: ApplicationFormField, value: str) -> None:
        """Reject type-confused values before an application adapter sees them.

        Adapters may still normalize a value or impose stricter business validation.  This
        layer only enforces the portable field contract and never claims that a syntactically
        valid value will be accepted by the application.
        """
        if field.kind == "choice":
            if value not in {choice.value for choice in field.choices}:
                raise BusinessToolRequestError(
                    f"{field.label} requires a declared choice value")
            return
        try:
            if field.kind == "integer" and not re.fullmatch(r"[-+]?\d+", value.strip()):
                raise ValueError
            if field.kind == "number" and not math.isfinite(float(value.strip())):
                raise ValueError
            if field.kind == "date":
                date.fromisoformat(value.strip())
            if field.kind == "datetime":
                datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if field.kind == "email":
                local, separator, domain = value.strip().rpartition("@")
                if (not separator or not local or "." not in domain
                        or domain.startswith(".") or domain.endswith(".")):
                    raise ValueError
            if field.kind == "url":
                parsed = urlsplit(value.strip())
                if (parsed.scheme not in {"http", "https"} or not parsed.netloc
                        or parsed.username is not None or parsed.password is not None):
                    raise ValueError
        except (ValueError, OverflowError):
            raise BusinessToolRequestError(
                f"{field.label} requires a valid {field.kind} value") from None

    @staticmethod
    def _score(query: str, field: ApplicationFormField) -> tuple[int, str]:
        terms = set(re.findall(r"[\w]+", query.casefold(), flags=re.UNICODE))
        text = f"{field.label} {field.reference.replace('_', ' ')}".casefold()
        return (-sum(term in text for term in terms), field.label.casefold())

    def describe(self, observation: PageObservation, args: object, *,
                 context: OperationContext | None = None) -> dict:
        if not isinstance(args, dict) or set(args) - {"field_query", "limit"}:
            raise BusinessToolRequestError(
                "describe_form accepts only field_query and limit")
        query = args.get("field_query", "")
        if not isinstance(query, str) or len(query) > 200 or _CONTROL.search(query):
            raise BusinessToolRequestError("field_query must be bounded text")
        limit = args.get("limit", 20)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise BusinessToolRequestError("limit must be an integer from 1 to 20")
        form = self._descriptor(observation, context)
        if form is None:
            return {"form_available": False,
                    "reason": "the application declares no supported form on this page"}
        binding = self._binding(form, observation, context)
        form_id = self._opaque("form_", binding)
        fields = list(self._visible(form, observation))
        if query.strip():
            fields = [item for item in fields
                      if self._score(query, item.descriptor)[0] < 0]
        fields.sort(key=lambda item: self._score(query, item.descriptor))
        shown = fields[:limit]
        return {
            "form_available": True,
            "form_id": form_id,
            "title": form.title,
            "fields": [{
                "field_id": self._opaque(
                    "form_field_", binding, item.descriptor.reference, item.control),
                "label": item.descriptor.label,
                "type": item.descriptor.kind,
                "control": item.control,
                "required": item.descriptor.required,
                **({"choices": [{"value": choice.value, "label": choice.label}
                                  for choice in item.descriptor.choices]}
                   if item.descriptor.choices else {}),
            } for item in shown],
            "visible_supported_field_count": len(fields),
            "returned_field_count": len(shown),
            "fields_truncated": len(shown) < len(fields),
            "scope": form.scope,
            "persistence": form.persistence,
            "submission": "user_only",
        }

    def prepare(self, observation: PageObservation, args: object, *,
                context: OperationContext | None = None) -> tuple[FormDraft, dict]:
        if not isinstance(args, dict) or set(args) != {"form_id", "values"}:
            raise BusinessToolRequestError("prepare_form requires form_id and values")
        form_id, raw_values = args.get("form_id"), args.get("values")
        if not isinstance(form_id, str) or not _OPAQUE_FORM.fullmatch(form_id):
            raise BusinessToolRequestError("form_id must come from describe_form")
        if (not isinstance(raw_values, list) or not 1 <= len(raw_values) <= 20
                or any(not isinstance(item, dict)
                       or set(item) != {"field_id", "value"} for item in raw_values)):
            raise BusinessToolRequestError(
                "values must contain 1-20 field_id/value objects")
        ids = [item.get("field_id") for item in raw_values]
        if (any(not isinstance(item, str) or not _OPAQUE_FIELD.fullmatch(item)
                for item in ids) or len(ids) != len(set(ids))):
            raise BusinessToolRequestError(
                "field_ids must be unique values from describe_form")
        form = self._descriptor(observation, context)
        if form is None:
            raise BusinessToolRequestError("the current page no longer has a supported form")
        binding = self._binding(form, observation, context)
        if not hmac.compare_digest(form_id, self._opaque("form_", binding)):
            raise BusinessToolRequestError(
                "form_id is absent or stale; describe the current form again")
        available = {
            self._opaque("form_field_", binding, item.descriptor.reference, item.control): item
            for item in self._visible(form, observation)
        }
        selected = []
        labels = []
        for item in raw_values:
            field_id, value = item["field_id"], item["value"]
            visible = available.get(field_id)
            if visible is None:
                raise BusinessToolRequestError(
                    "field_id is absent or stale; describe the current form again")
            if not isinstance(value, str) or len(value) > 4000 or _CONTROL.search(value):
                raise BusinessToolRequestError(
                    f"{visible.descriptor.label} requires bounded text")
            self._validate_value(visible.descriptor, value)
            selected.append(ApplicationFormValue(
                visible.descriptor, value, visible.control))
            labels.append(visible.descriptor.label)
        try:
            draft = self._port.prepare_form(form, tuple(selected), context=context)
        except (BusinessToolContractError, BusinessToolRequestError):
            raise
        except (TypeError, ValueError) as exc:
            raise BusinessToolContractError(
                "the application returned an invalid form draft") from exc
        if not isinstance(draft, FormDraft):
            raise BusinessToolContractError("the application returned an invalid form draft")
        expected = {(item.field.reference, item.control) for item in selected}
        actual = {(item.field, item.control) for item in draft.fields}
        if (draft.route != form.route or expected != actual
                or draft.persistence != form.persistence
                or draft.effect != form.effect or draft.targets != form.targets):
            raise BusinessToolContractError(
                "the application form draft exceeds the current descriptor")
        return draft, {
            "draft_ready": True,
            "title": form.title,
            "fields": labels,
            "field_count": len(labels),
            "persistence": draft.persistence,
            "submission": "user_only",
        }


__all__ = [
    "ApplicationFormDescriptor", "ApplicationFormField", "ApplicationFormPort",
    "ApplicationFormTools", "ApplicationFormValue", "DESCRIBE_FORM_SCHEMA",
    "DESCRIBE_FORM_TOOL", "FORM_TOOL_SCHEMAS", "FORM_TOOLS", "FormChoice",
    "NoApplicationForms", "PREPARE_FORM_SCHEMA", "PREPARE_FORM_TOOL",
]
