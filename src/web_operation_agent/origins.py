"""What an origin is, written once.

An origin decides whether a page may drive a session, so the same string has to mean the
same thing everywhere it is compared. A browser's `location.origin` is already canonical;
a deployment that writes ``https://App.Example:443`` in its configuration is the same
place and must not be accepted by one module and refused by another.

Four modules used to answer this with four sets of rules (P7.5 D-13): the application
descriptor casefolded the host and dropped a default port, the gateway lowercased and
dropped it, the session channel kept the netloc exactly as it arrived, and the wire schema
only validated. Nothing could be bypassed by the difference -- every mismatch fails closed
-- but the same concept had four implementations, and only one of them was checked
whenever one of them changed.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

#: Long enough for any real origin, short enough that no caller has to bound it again.
MAX_ORIGIN_LENGTH = 2048

#: Where plain HTTP is the developer's own machine rather than a downgrade.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class OriginError(ValueError):
    """The value is not an origin this product will compare.

    A `ValueError`, so callers that already refuse bad configuration that way keep
    working; callers that answer a wire request translate it into their own refusal.
    """


def _canonical(value: object, *, name: str, allow_path: bool,
               require_secure_unless_loopback: bool) -> str:
    if not isinstance(value, str):
        raise OriginError(f"{name} must be a string")
    if not value or len(value) > MAX_ORIGIN_LENGTH:
        raise OriginError(f"{name} must be a bounded absolute http(s) origin")
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise OriginError(f"{name} must be an absolute http(s) origin")
    if parsed.username is not None or parsed.password is not None:
        raise OriginError(f"{name} must not contain user information")
    if not allow_path and (parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise OriginError(f"{name} must not contain a path, query, or fragment")
    try:
        port = parsed.port
    except ValueError as exc:
        raise OriginError(f"{name} port is invalid") from exc
    scheme = parsed.scheme.lower()
    host = parsed.hostname.casefold()
    if (require_secure_unless_loopback and scheme == "http" and host not in LOOPBACK_HOSTS):
        raise OriginError(f"plain HTTP {name} must use loopback")
    # An IPv6 literal is written with its brackets; `hostname` strips them.
    rendered = f"[{host}]" if ":" in host else host
    default_port = 80 if scheme == "http" else 443
    suffix = "" if port is None or port == default_port else f":{port}"
    return f"{scheme}://{rendered}{suffix}"


def normalize_origin(value: object, *, name: str = "origin",
                     require_secure_unless_loopback: bool = False) -> str:
    """The canonical form of an origin, which must be nothing but an origin."""
    return _canonical(value, name=name, allow_path=False,
                      require_secure_unless_loopback=require_secure_unless_loopback)


def origin_of(value: object, *, name: str = "URL") -> str:
    """The canonical origin of an absolute http(s) URL; its path and query are not part of it."""
    return _canonical(value, name=name, allow_path=True,
                      require_secure_unless_loopback=False)


#: A same-origin path the Runtime posts `/begin` and `/end` under; see `page_write_guard`.
PAGE_WRITE_GUARD_PATH = re.compile(r"/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+){0,7}\Z")


def validate_page_write_guard(value: object) -> str:
    """An empty string, or a same-origin absolute path of at most 128 characters."""
    if value == "":
        return ""
    if (not isinstance(value, str) or len(value) > 128
            or not PAGE_WRITE_GUARD_PATH.fullmatch(value)
            or any(not segment.strip(".") for segment in value.split("/")[1:])):
        raise ValueError("page_write_guard must be a same-origin absolute path")
    return value
