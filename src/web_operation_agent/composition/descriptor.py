"""The versioned, non-secret facts one application publishes for a release.

A deployment pins an application by its ``ApplicationDescriptor``: which application id,
where it lives, and which tool-catalog digest its Skill declares. The identifier grammar
is defined here because it bounds every adapter-declared identity downstream.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from ..origins import normalize_origin, validate_page_write_guard


APPLICATION_ADAPTER_VERSION = 2
_APPLICATION_ID = re.compile(r"[a-z][a-z0-9.-]{2,79}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _origin(value: object) -> str:
    """The application's own origin: the strict form, and plain HTTP only on loopback."""
    return normalize_origin(value, name="application origin",
                            require_secure_unless_loopback=True)


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
    #: Where the application makes the signed-in session read-only while the Agent acts on
    #: the page: the Runtime posts `{"ttl_s": n}` to `<path>/begin` before every page action
    #: and expects `{"active": true, "expires_at": <epoch seconds>}`, refusing the action
    #: otherwise, and posts to `<path>/end` when the turn ends. Empty for an application
    #: without one.
    page_write_guard: str = ""

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
        validate_page_write_guard(self.page_write_guard)
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
            **({"page_write_guard": self.page_write_guard} if self.page_write_guard else {}),
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

    def owns_url(self, value: str) -> bool:
        """Return whether a browser URL belongs to the descriptor's exact web origin."""
        if not isinstance(value, str):
            return False
        parsed = urlsplit(value)
        try:
            candidate = _origin(f"{parsed.scheme}://{parsed.netloc}")
        except ValueError:
            return False
        return candidate == self.origin
