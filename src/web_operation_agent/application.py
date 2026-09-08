"""Versioned boundary between the generic Agent host and one web application."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from .business import BusinessToolRuntime


APPLICATION_ADAPTER_VERSION = 1
_APPLICATION_ID = re.compile(r"[a-z][a-z0-9.-]{2,79}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _origin(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("application origin must be a string")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ValueError("application origin must be an http(s) origin")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("plain HTTP application origins must use loopback")
    return f"{parsed.scheme}://{parsed.netloc}"


@dataclass(frozen=True)
class ApplicationDescriptor:
    """Immutable, non-secret facts used to bind an adapter to a product session."""

    schema_version: int
    application_id: str
    display_name: str
    origin: str
    navigation_mode: str
    page_controller_module: str
    catalog_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != APPLICATION_ADAPTER_VERSION:
            raise ValueError("application descriptor version is unsupported")
        if not isinstance(self.application_id, str) \
                or not _APPLICATION_ID.fullmatch(self.application_id):
            raise ValueError("application_id is invalid")
        if not isinstance(self.display_name, str) or not self.display_name.strip() \
                or len(self.display_name.strip()) > 100:
            raise ValueError("application display_name is invalid")
        normalized_origin = _origin(self.origin)
        if self.navigation_mode not in {"document", "spa"}:
            raise ValueError("application navigation_mode is invalid")
        if not isinstance(self.page_controller_module, str):
            raise ValueError("page_controller_module must be a URL")
        module = urlsplit(self.page_controller_module)
        module_origin = _origin(f"{module.scheme}://{module.netloc}") \
            if module.scheme and module.netloc else ""
        if (module_origin != normalized_origin or not module.path.startswith("/")
                or module.username is not None or module.password is not None
                or module.query or module.fragment):
            raise ValueError("page_controller_module must be a same-origin URL")
        if not isinstance(self.catalog_sha256, str) \
                or not _SHA256.fullmatch(self.catalog_sha256):
            raise ValueError("catalog_sha256 is invalid")
        object.__setattr__(self, "display_name", self.display_name.strip())
        object.__setattr__(self, "origin", normalized_origin)

    def to_json(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "application_id": self.application_id,
            "display_name": self.display_name,
            "origin": self.origin,
            "navigation_mode": self.navigation_mode,
            "page_controller_module": self.page_controller_module,
            "catalog_sha256": self.catalog_sha256,
        }

    def validate_page_controller(self, value: str) -> str:
        """Validate a deployment-selected controller against this application origin."""
        if not isinstance(value, str):
            raise ValueError("page_controller_module must be a URL")
        module = urlsplit(value)
        module_origin = _origin(f"{module.scheme}://{module.netloc}") \
            if module.scheme and module.netloc else ""
        if (module_origin != self.origin or not module.path.startswith("/")
                or module.username is not None or module.password is not None
                or module.query or module.fragment):
            raise ValueError("page_controller_module must be a same-origin URL")
        return value


@runtime_checkable
class ApplicationAdapter(Protocol):
    """Minimum application-specific surface consumed by the generic composition."""

    @property
    def descriptor(self) -> ApplicationDescriptor: ...

    @property
    def business_tools(self) -> BusinessToolRuntime: ...

    def health(self) -> dict: ...


@dataclass(frozen=True)
class BoundApplication:
    descriptor: ApplicationDescriptor
    business_tools: BusinessToolRuntime
    catalog: dict


def bind_application(adapter: ApplicationAdapter) -> BoundApplication:
    """Validate an adapter once before any user task reaches the Agent loop."""
    if not isinstance(adapter, ApplicationAdapter):
        raise TypeError("application adapter does not implement the required protocol")
    descriptor = adapter.descriptor
    if not isinstance(descriptor, ApplicationDescriptor):
        raise TypeError("application adapter descriptor is invalid")
    tools = adapter.business_tools
    if not isinstance(tools, BusinessToolRuntime):
        raise TypeError("application adapter business_tools is invalid")
    if tools.application_id and tools.application_id != descriptor.application_id:
        raise ValueError("application descriptor and business tools use different identities")
    if descriptor.catalog_sha256 != tools.catalog_sha256:
        raise ValueError("application descriptor is stale for its Skill and tool catalog")
    return BoundApplication(
        descriptor=descriptor,
        business_tools=tools,
        catalog=tools.catalog_manifest,
    )
