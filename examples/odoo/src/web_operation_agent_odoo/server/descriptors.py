"""Validated runtime descriptions supplied by the installed Odoo module.

The application add-on performs enumeration because ordinary delegated users cannot search
Odoo's ``ir.model`` or ``ir.actions.act_window`` tables.  This module is the other half of
that boundary: it rejects malformed, duplicated or internally inconsistent metadata before
the server may index it.  A catalog is discovery only; it grants no business-data read and
cannot authorize a mutation.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from web_operation_agent.business.runtime import BusinessToolContractError


_MODEL = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,127}\Z")
_XML_ID = re.compile(r"[A-Za-z0-9_.-]{0,200}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_OPERATION_ORDER = ("create", "read", "write", "unlink")
_OPERATIONS = frozenset(_OPERATION_ORDER)
_VIEW_MODE = re.compile(r"[a-z][a-z0-9_-]{0,39}\Z")
_MAX_MODELS = 5_000
_MAX_ACTIONS = 5_000
_MAX_MODULES = 5_000


def _text(value: object, *, maximum: int, allow_empty: bool = True) -> str:
    if (not isinstance(value, str) or len(value) > maximum
            or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value)
            or (not allow_empty and not value.strip())):
        raise BusinessToolContractError("Odoo returned malformed runtime metadata")
    return value


def _route(value: object) -> str:
    route = _text(value, maximum=200, allow_empty=False)
    if (route.startswith(("/", ".")) or "\\" in route or "%" in route
            or ".." in route.split("/") or "//" in route):
        raise BusinessToolContractError("Odoo returned an unsafe action route")
    return route


def _catalog_digest(models: list[dict], actions: list[dict],
                    omitted_models: list[str] | None = None,
                    omitted_actions: list[int] | None = None) -> str:
    body = json.dumps(
        {"models": models, "actions": actions,
         "omitted_models": list(omitted_models or ()),
         "omitted_actions": list(omitted_actions or ())}, ensure_ascii=False,
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(body).hexdigest()


@dataclass(frozen=True, slots=True)
class OdooModelDescriptor:
    model: str
    label: str
    operations: tuple[str, ...]
    transient: bool


@dataclass(frozen=True, slots=True)
class OdooActionDescriptor:
    action_id: int
    xml_id: str
    name: str
    route: str
    model: str
    readable: bool
    view_modes: tuple[str, ...]
    breadcrumbs: tuple[str, ...]
    domain: str
    context: str


@dataclass(frozen=True, slots=True)
class OdooInstalledModule:
    name: str
    version: str


@dataclass(frozen=True, slots=True)
class OdooSourceManifest:
    schema_version: int
    odoo_release: str
    module_digest: str
    modules: tuple[OdooInstalledModule, ...]

    @classmethod
    def from_payload(cls, value: object) -> "OdooSourceManifest":
        if not isinstance(value, dict) or set(value) != {
                "schema_version", "odoo_release", "module_digest", "modules"} \
                or value.get("schema_version") != 1:
            raise BusinessToolContractError("Odoo returned an invalid source manifest")
        raw = value.get("modules")
        if not isinstance(raw, list) or not 1 <= len(raw) <= _MAX_MODULES:
            raise BusinessToolContractError("Odoo returned an invalid source manifest")
        modules = []
        for row in raw:
            if not isinstance(row, dict) or set(row) != {"name", "version"}:
                raise BusinessToolContractError("Odoo returned an invalid source manifest")
            name = row["name"]
            if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9_]{1,64}", name):
                raise BusinessToolContractError("Odoo returned an invalid source manifest")
            modules.append(OdooInstalledModule(
                name=name, version=_text(row["version"], maximum=100)))
        if [item.name for item in modules] != sorted({item.name for item in modules}):
            raise BusinessToolContractError("Odoo source manifest modules are not canonical")
        body = json.dumps([(item.name, item.version) for item in modules],
                          ensure_ascii=False, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        if value.get("module_digest") != digest:
            raise BusinessToolContractError("Odoo source manifest digest did not verify")
        return cls(
            schema_version=1,
            odoo_release=_text(value["odoo_release"], maximum=100, allow_empty=False),
            module_digest=digest,
            modules=tuple(modules),
        )


@dataclass(frozen=True, slots=True)
class OdooRuntimeCatalog:
    schema_version: int
    odoo_release: str
    language: str
    company_id: int
    module_digest: str
    descriptor_digest: str
    models: tuple[OdooModelDescriptor, ...]
    actions: tuple[OdooActionDescriptor, ...]
    omitted_models: tuple[str, ...]
    omitted_actions: tuple[int, ...]

    @property
    def complete(self) -> bool:
        return not self.omitted_models and not self.omitted_actions

    @classmethod
    def from_payload(cls, value: object) -> "OdooRuntimeCatalog":
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise BusinessToolContractError("Odoo returned an unsupported descriptor catalog")
        raw_models = value.get("models")
        raw_actions = value.get("actions")
        omitted_models = value.get("omitted_models")
        omitted_actions = value.get("omitted_actions")
        if (not isinstance(raw_models, list) or len(raw_models) > _MAX_MODELS
                or not isinstance(raw_actions, list) or len(raw_actions) > _MAX_ACTIONS
                or not isinstance(omitted_models, list)
                or len(omitted_models) > _MAX_MODELS
                or not isinstance(omitted_actions, list)
                or len(omitted_actions) > _MAX_ACTIONS):
            raise BusinessToolContractError("Odoo returned an oversized descriptor catalog")
        if (omitted_models != sorted(set(omitted_models))
                or any(not isinstance(name, str) or not _MODEL.fullmatch(name)
                       for name in omitted_models)
                or omitted_actions != sorted(set(omitted_actions))
                or any(isinstance(action_id, bool) or not isinstance(action_id, int)
                       or action_id <= 0 for action_id in omitted_actions)):
            raise BusinessToolContractError("Odoo returned malformed descriptor omissions")
        if not _DIGEST.fullmatch(str(value.get("module_digest") or "")):
            raise BusinessToolContractError("Odoo returned an invalid module digest")
        claimed_digest = str(value.get("descriptor_digest") or "")
        if not _DIGEST.fullmatch(claimed_digest) \
                or claimed_digest != _catalog_digest(
                    raw_models, raw_actions, omitted_models, omitted_actions):
            raise BusinessToolContractError("Odoo descriptor catalog digest did not verify")

        models = tuple(cls._model(row) for row in raw_models)
        actions = tuple(cls._action(row) for row in raw_actions)
        if len({item.model for item in models}) != len(models):
            raise BusinessToolContractError("Odoo descriptor catalog repeats a model")
        if len({item.action_id for item in actions}) != len(actions):
            raise BusinessToolContractError("Odoo descriptor catalog repeats an action")
        readable_models = {item.model for item in models}
        if any(action.readable and action.model not in readable_models for action in actions):
            raise BusinessToolContractError(
                "Odoo descriptor marks an unknown action model readable")

        company_id = value.get("company_id")
        if isinstance(company_id, bool) or not isinstance(company_id, int) or company_id <= 0:
            raise BusinessToolContractError("Odoo descriptor company is invalid")
        return cls(
            schema_version=1,
            odoo_release=_text(value.get("odoo_release"), maximum=100, allow_empty=False),
            language=_text(value.get("language"), maximum=64),
            company_id=company_id,
            module_digest=value["module_digest"],
            descriptor_digest=claimed_digest,
            models=models,
            actions=actions,
            omitted_models=tuple(omitted_models),
            omitted_actions=tuple(omitted_actions),
        )

    @staticmethod
    def _model(row: object) -> OdooModelDescriptor:
        if not isinstance(row, dict) or set(row) != {
                "model", "label", "operations", "transient"}:
            raise BusinessToolContractError("Odoo returned a malformed model descriptor")
        model = row["model"]
        operations = row["operations"]
        if (not isinstance(model, str) or not _MODEL.fullmatch(model)
                or not isinstance(operations, list)
                or not operations or operations != [
                    item for item in _OPERATION_ORDER if item in operations]
                or len(operations) != len(set(operations))
                or not set(operations) <= _OPERATIONS
                or "read" not in operations
                or not isinstance(row["transient"], bool)):
            raise BusinessToolContractError("Odoo returned a malformed model descriptor")
        return OdooModelDescriptor(
            model=model,
            label=_text(row["label"], maximum=200, allow_empty=False),
            operations=tuple(operations),
            transient=row["transient"],
        )

    @staticmethod
    def _action(row: object) -> OdooActionDescriptor:
        expected = {"action_id", "xml_id", "name", "route", "model", "readable",
                    "view_modes", "breadcrumbs", "domain", "context"}
        if not isinstance(row, dict) or set(row) != expected:
            raise BusinessToolContractError("Odoo returned a malformed action descriptor")
        action_id = row["action_id"]
        xml_id = row["xml_id"]
        model = row["model"]
        modes = row["view_modes"]
        breadcrumbs = row["breadcrumbs"]
        if (isinstance(action_id, bool) or not isinstance(action_id, int) or action_id <= 0
                or not isinstance(xml_id, str) or not _XML_ID.fullmatch(xml_id)
                or not isinstance(model, str) or not _MODEL.fullmatch(model)
                or not isinstance(row["readable"], bool)
                or not isinstance(modes, list) or len(modes) > 20
                or len(modes) != len(set(modes))
                or any(not isinstance(item, str) or not _VIEW_MODE.fullmatch(item)
                       for item in modes)
                or not isinstance(breadcrumbs, list) or len(breadcrumbs) > 20
                or breadcrumbs != sorted(set(breadcrumbs))):
            raise BusinessToolContractError("Odoo returned a malformed action descriptor")
        return OdooActionDescriptor(
            action_id=action_id,
            xml_id=xml_id,
            name=_text(row["name"], maximum=200),
            route=_route(row["route"]),
            model=model,
            readable=row["readable"],
            view_modes=tuple(modes),
            breadcrumbs=tuple(_text(item, maximum=1_000, allow_empty=False)
                              for item in breadcrumbs),
            domain=_text(row["domain"], maximum=2_000),
            context=_text(row["context"], maximum=2_000),
        )


class OdooRuntimeDescriptorReader:
    """Read one current catalog through the already delegated Odoo executor."""

    def __init__(self, executor):
        if not callable(getattr(executor, "call", None)):
            raise TypeError("Odoo descriptor reader requires an RPC executor")
        self._executor = executor

    def catalog(self) -> OdooRuntimeCatalog:
        value = self._executor.call("woa.runtime.descriptor", "catalog", [], {})
        return OdooRuntimeCatalog.from_payload(value)

    def source_manifest(self) -> OdooSourceManifest:
        value = self._executor.call("woa.runtime.descriptor", "source_manifest", [], {})
        return OdooSourceManifest.from_payload(value)
