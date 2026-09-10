"""Versioned boundary between the generic Agent host and one web application."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from urllib.parse import parse_qsl, urljoin, urlsplit

from .business import (BusinessDataClassifier, BusinessPostconditionVerifier,
                       BusinessRouteValidator, BusinessToolResult, BusinessToolRuntime,
                       MutationPostcondition)
from .identity import IdentityAssertion, PrincipalContext, PrincipalError


APPLICATION_ADAPTER_VERSION = 2
_APPLICATION_ID = re.compile(r"[a-z][a-z0-9.-]{2,79}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PORT_NAME = re.compile(r"[a-z][a-z0-9_.-]{1,63}\Z")
_SAFE_ROUTE = re.compile(r"^/[^\\\x00-\x20\x7f]*$")


def _origin(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("application origin must be a string")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ValueError("application origin must be an http(s) origin")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("application origin port is invalid") from exc
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("plain HTTP application origins must use loopback")
    host = parsed.hostname.casefold()
    rendered_host = f"[{host}]" if ":" in host else host
    default_port = 80 if parsed.scheme == "http" else 443
    suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{parsed.scheme}://{rendered_host}{suffix}"


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


@runtime_checkable
class ApplicationIdentityPort(Protocol):
    """Bind and validate an application-server identity assertion."""

    @property
    def required(self) -> bool: ...

    @property
    def required_scopes(self) -> frozenset[str]: ...

    def bind(self, page_identity: IdentityAssertion, *, delegation_id: str,
             lifetime_s: int = 300, now_ms: int | None = None) -> PrincipalContext: ...

    def validate(self, principal: PrincipalContext | None, *,
                 application_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class AnonymousIdentityPort:
    """Explicit identity policy for a synthetic application with no authenticated actor."""

    required: bool = False
    required_scopes: frozenset[str] = frozenset()

    def bind(self, page_identity: IdentityAssertion, *, delegation_id: str,
             lifetime_s: int = 300, now_ms: int | None = None) -> PrincipalContext:
        raise PrincipalError("this application does not bind authenticated identities")

    def validate(self, principal: PrincipalContext | None, *,
                 application_id: str) -> None:
        if principal is not None:
            principal.require_live(application_id=application_id)


@runtime_checkable
class ApplicationRoutePort(BusinessRouteValidator, Protocol):
    """Validate and normalize application routes exposed by typed tools."""

    @property
    def application_id(self) -> str: ...

    @property
    def session_handoff_query_keys(self) -> frozenset[str]: ...

    def validate_tool_route(self, route: str) -> str: ...

    def route_identity(self, url: str) -> str: ...

    def contract_route_identity(self, url: str) -> tuple[str, str]: ...

    def discover_source_scopes(self, observation: object) -> frozenset[str]: ...

    def validate_application_entry(self, ref: object, observation: object) -> tuple[str, str]: ...


@dataclass(frozen=True, slots=True)
class DeclaredRoutePort:
    """Executable route boundary for paths emitted by an application's typed tools."""

    application_id: str
    tool_route_patterns: tuple[str, ...]
    session_handoff_query_keys: frozenset[str] = frozenset()
    allowed_tool_query_keys: frozenset[str] = frozenset()
    contract_route_pattern: str = ""
    application_entry_patterns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _APPLICATION_ID.fullmatch(self.application_id):
            raise ValueError("route port application_id is invalid")
        if (not isinstance(self.tool_route_patterns, tuple)
                or any(not isinstance(pattern, str) or not pattern
                       for pattern in self.tool_route_patterns)):
            raise ValueError("route port patterns must be a tuple of regular expressions")
        for pattern in self.tool_route_patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError("route port pattern is invalid") from exc
        if not isinstance(self.contract_route_pattern, str):
            raise ValueError("contract route pattern must be a string")
        if (not isinstance(self.application_entry_patterns, tuple)
                or any(not isinstance(pattern, str) or not pattern
                       for pattern in self.application_entry_patterns)):
            raise ValueError("application entry patterns must be a tuple of regular expressions")
        if self.contract_route_pattern:
            try:
                compiled = re.compile(self.contract_route_pattern)
            except re.error as exc:
                raise ValueError("contract route pattern is invalid") from exc
            if not {"scope", "route"}.issubset(compiled.groupindex):
                raise ValueError("contract route pattern requires scope and route groups")
        for pattern in self.application_entry_patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError("application entry pattern is invalid") from exc
        if (not isinstance(self.session_handoff_query_keys, frozenset)
                or not isinstance(self.allowed_tool_query_keys, frozenset)):
            raise ValueError("route port handoff keys are invalid")
        handoff = frozenset(
            key.casefold() if isinstance(key, str) else key
            for key in self.session_handoff_query_keys)
        allowed = frozenset(
            key.casefold() if isinstance(key, str) else key
            for key in self.allowed_tool_query_keys)
        if (any(not isinstance(key, str) or not _PORT_NAME.fullmatch(key)
                for key in handoff | allowed) or handoff & allowed):
            raise ValueError("route port handoff keys are invalid")
        object.__setattr__(self, "session_handoff_query_keys", handoff)
        object.__setattr__(self, "allowed_tool_query_keys", allowed)

    def validate_tool_route(self, route: str) -> str:
        parsed = urlsplit(route) if isinstance(route, str) else None
        safe = bool(
            parsed and _SAFE_ROUTE.fullmatch(route) and not route.startswith("//")
            and not parsed.scheme and not parsed.netloc and not parsed.fragment
            and len(route) <= 1_000 and "%" not in parsed.path
            and ".." not in parsed.path.split("/"))
        if not safe:
            raise ValueError("application tool route must be a safe relative path")
        try:
            query = parse_qsl(
                parsed.query, keep_blank_values=True, strict_parsing=True,
                max_num_fields=20)
        except ValueError as exc:
            raise ValueError("application tool route query is invalid") from exc
        query_keys = [key.casefold() for key, _value in query]
        if (len(query_keys) != len(set(query_keys))
                or any(key in self.session_handoff_query_keys for key in query_keys)
                or any(key not in self.allowed_tool_query_keys for key in query_keys)):
            raise ValueError("application tool route query is outside the adapter contract")
        path = parsed.path
        if not any(re.fullmatch(pattern, path) for pattern in self.tool_route_patterns):
            raise ValueError("application tool route does not match the adapter contract")
        return route

    def route_identity(self, url: str) -> str:
        parsed = urlsplit(url) if isinstance(url, str) else None
        if not parsed or not parsed.path.startswith("/"):
            return ""
        return parsed.path.rstrip("/") or "/"

    def contract_route_identity(self, url: str) -> tuple[str, str]:
        """Return the adapter's legacy source-contract route identity, when configured."""
        if not self.contract_route_pattern or not isinstance(url, str):
            return "", ""
        match = re.search(self.contract_route_pattern, urlsplit(url).path)
        if match is None:
            return "", ""
        return match.group("scope").casefold(), match.group("route").rstrip("/").casefold()

    def discover_source_scopes(self, observation: object) -> frozenset[str]:
        scopes = set()
        current_url = str(getattr(observation, "url", ""))
        current = urlsplit(current_url)
        for element in getattr(observation, "elements", ()):
            href = getattr(element, "attrs", {}).get("href")
            if not isinstance(href, str):
                continue
            target = urlsplit(urljoin(current_url, href))
            if target.scheme != current.scheme or target.netloc != current.netloc:
                continue
            scope, route = self.contract_route_identity(target.geturl())
            if scope and route:
                scopes.add(scope)
        return frozenset(scopes)

    def validate_application_entry(
            self, ref: object, observation: object) -> tuple[str, str]:
        if not isinstance(ref, str) or not ref:
            return "", "ref must identify an observed link"
        matches = [element for element in getattr(observation, "elements", ())
                   if getattr(element, "ref", "") == ref]
        if (len(matches) != 1 or getattr(matches[0], "role", "") != "link"
                or getattr(matches[0], "tag", "") != "a"):
            return "", "ref must identify one observed anchor link"
        href = getattr(matches[0], "attrs", {}).get("href")
        if not isinstance(href, str) or not href:
            return "", "the observed link has no href"
        current = urlsplit(str(getattr(observation, "url", "")))
        target = urlsplit(urljoin(current.geturl(), href))
        same_origin = (target.scheme in {"http", "https"}
                       and target.scheme == current.scheme and target.netloc == current.netloc)
        entry_path = any(re.fullmatch(pattern, target.path)
                         for pattern in self.application_entry_patterns)
        if not same_origin or not entry_path or target.fragment:
            return "", "the link is outside the adapter's application entry contract"
        query = parse_qsl(target.query, keep_blank_values=True)
        if self.session_handoff_query_keys:
            valid_query = bool(query) and all(
                key.casefold() in self.session_handoff_query_keys
                and value.casefold() == "[redacted]" for key, value in query)
        else:
            valid_query = not query
        if not valid_query:
            return "", "the application link lacks an adapter-declared redacted handoff"
        return ref, ""


@dataclass(frozen=True, slots=True)
class DeclaredDataClassifier:
    """Classify each typed tool with a bounded, adapter-owned declaration."""

    declarations: tuple[tuple[str, tuple[str, ...]], ...]

    def __post_init__(self) -> None:
        names = [name for name, _classes in self.declarations]
        if len(names) != len(set(names)):
            raise ValueError("data classifier tool names must be unique")
        for name, classes in self.declarations:
            if (not _PORT_NAME.fullmatch(name) or not classes
                    or len(classes) != len(set(classes))
                    or any(not _PORT_NAME.fullmatch(item) for item in classes)):
                raise ValueError("data classifier declaration is invalid")

    @property
    def tool_names(self) -> frozenset[str]:
        return frozenset(name for name, _classes in self.declarations)

    def classify(self, name: str, result: BusinessToolResult) -> tuple[str, ...]:
        if not isinstance(result, BusinessToolResult):
            raise TypeError("data classifier requires a BusinessToolResult")
        declared = dict(self.declarations).get(name)
        if declared is None:
            raise ValueError("tool is absent from the application data contract")
        return tuple(sorted(declared))


@dataclass(frozen=True, slots=True)
class ResultPostconditionVerifier:
    """Require adapter-declared truthy result fields after a mutation acknowledgement."""

    requirements: tuple[tuple[str, tuple[str, ...]], ...]

    def __post_init__(self) -> None:
        names = [name for name, _paths in self.requirements]
        if len(names) != len(set(names)):
            raise ValueError("postcondition tool names must be unique")
        for name, paths in self.requirements:
            if (not _PORT_NAME.fullmatch(name) or not paths
                    or any(not isinstance(path, str) or not path.startswith(("content.", "audit."))
                           for path in paths)):
                raise ValueError("postcondition requirements are invalid")

    @property
    def supported_tools(self) -> frozenset[str]:
        return frozenset(name for name, _paths in self.requirements)

    def verify(self, name: str, args: dict, result: BusinessToolResult, *,
               context=None) -> MutationPostcondition:
        paths = dict(self.requirements).get(name)
        if paths is None:
            raise ValueError("mutation is absent from the postcondition contract")
        roots = {"content": result.content, "audit": result.audit}
        verified = True
        for path in paths:
            root_name, *segments = path.split(".")
            value = roots[root_name]
            for segment in segments:
                value = value.get(segment) if isinstance(value, dict) else None
            verified = verified and value is True
        return MutationPostcondition(
            verified=verified,
            code="verified_readback" if verified else "unverified_readback",
            evidence={"assertions": list(paths), "result_digest": result.content_digest},
        )


@dataclass(frozen=True, slots=True)
class MutationReconciliation:
    outcome: str
    requires_human: bool
    reason: str

    def __post_init__(self) -> None:
        if self.outcome not in {"completed", "failed", "unknown"}:
            raise ValueError("mutation reconciliation outcome is invalid")
        if not isinstance(self.requires_human, bool):
            raise ValueError("mutation reconciliation requires_human must be boolean")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("mutation reconciliation reason is invalid")


@runtime_checkable
class ApplicationMutationReconciler(Protocol):
    """Read-only application check for an earlier uncertain mutation."""

    @property
    def supported_tools(self) -> frozenset[str]: ...

    def reconcile(self, name: str, business_key: dict, *, evidence: dict,
                  principal: PrincipalContext | None) -> MutationReconciliation: ...


@dataclass(frozen=True, slots=True)
class ConservativeMutationReconciler:
    """Explicitly keep uncertain writes unresolved until an application check exists."""

    supported_tools: frozenset[str] = frozenset()

    def reconcile(self, name: str, business_key: dict, *, evidence: dict,
                  principal: PrincipalContext | None) -> MutationReconciliation:
        if name not in self.supported_tools:
            raise ValueError("mutation is absent from the reconciliation contract")
        return MutationReconciliation(
            outcome="unknown", requires_human=True,
            reason="the adapter has not established a safe automatic reconciliation",
        )


@dataclass(frozen=True, slots=True)
class ApplicationPorts:
    identity: ApplicationIdentityPort
    routes: ApplicationRoutePort
    postconditions: BusinessPostconditionVerifier
    reconciliation: ApplicationMutationReconciler
    data: BusinessDataClassifier

    def __post_init__(self) -> None:
        checks = (
            isinstance(self.identity, ApplicationIdentityPort),
            isinstance(self.routes, ApplicationRoutePort),
            isinstance(self.postconditions, BusinessPostconditionVerifier),
            isinstance(self.reconciliation, ApplicationMutationReconciler),
            isinstance(self.data, BusinessDataClassifier),
        )
        if not all(checks):
            raise TypeError("application ports do not implement the required contracts")


@runtime_checkable
class ApplicationAdapter(Protocol):
    """Minimum application-specific surface consumed by the generic composition."""

    @property
    def descriptor(self) -> ApplicationDescriptor: ...

    @property
    def business_tools(self) -> BusinessToolRuntime: ...

    @property
    def ports(self) -> ApplicationPorts: ...

    def health(self) -> dict: ...


@dataclass(frozen=True)
class BoundApplication:
    descriptor: ApplicationDescriptor
    business_tools: BusinessToolRuntime
    catalog: dict
    ports: ApplicationPorts


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
    ports = adapter.ports
    if not isinstance(ports, ApplicationPorts):
        raise TypeError("application adapter ports are invalid")
    if ports.routes.application_id != descriptor.application_id:
        raise ValueError("route port belongs to another application")
    if ports.data.tool_names != tools.tool_names:
        raise ValueError("data port must classify every business tool")
    if ports.postconditions.supported_tools != tools.mutation_tool_names:
        raise ValueError("postcondition port must verify every mutation tool")
    if ports.reconciliation.supported_tools != tools.mutation_tool_names:
        raise ValueError("reconciliation port must cover every mutation tool")
    declared_scopes = frozenset(scope for scope in tools.required_scopes.values() if scope)
    if ports.identity.required_scopes != declared_scopes:
        raise ValueError("identity port scopes differ from the business tool catalog")
    if tools.data_classifier is not ports.data:
        raise ValueError("business runtime is not bound to the adapter data port")
    if tools.postcondition_verifier is not ports.postconditions:
        raise ValueError("business runtime is not bound to the adapter postcondition port")
    if tools.route_validator is not ports.routes:
        raise ValueError("business runtime is not bound to the adapter route port")
    return BoundApplication(
        descriptor=descriptor,
        business_tools=tools,
        catalog=tools.catalog_manifest,
        ports=ports,
    )
