"""The executable ports a session may demand of an application, and their defaults.

Identity, routes, current forms, data classification, mutation postconditions and
reconciliation are the application-specific contracts behind which the generic Agent acts;
``ApplicationPorts`` bundles them so a binding can check them once. The ``Declared*`` and
``Result*`` classes are the adapter-owned bounded declarations the core itself ships.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from urllib.parse import parse_qsl, urljoin, urlsplit

from ..business.runtime import (BusinessDataClassifier, BusinessPostconditionVerifier,
                               BusinessRouteValidator, BusinessToolResult,
                               MutationPostcondition)
from ..business.forms import ApplicationFormPort, NoApplicationForms
from ..state.identity import IdentityAssertion, PrincipalContext, PrincipalError
from .descriptor import _APPLICATION_ID


_PORT_NAME = re.compile(r"[a-z][a-z0-9_.-]{1,63}\Z")
_SAFE_ROUTE = re.compile(r"^/[^\\\x00-\x20\x7f]*$")


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

    def compose_contract_route(self, scope: str, route: str) -> str: ...

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
    contract_route_template: str = ""
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
        if not isinstance(self.contract_route_template, str):
            raise ValueError("contract route template must be a string")
        if bool(self.contract_route_pattern) != bool(self.contract_route_template):
            raise ValueError(
                "contract route pattern and template must be configured together")
        if self.contract_route_template and (
                "{scope}" not in self.contract_route_template
                or "{route}" not in self.contract_route_template):
            raise ValueError("contract route template requires {scope} and {route}")
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

    def compose_contract_route(self, scope: str, route: str) -> str:
        """The inverse of `contract_route_identity`: build the route for one (scope, route)."""
        if not self.contract_route_template:
            raise ValueError("this adapter declares no contract route template")
        if not isinstance(scope, str) or not scope or not isinstance(route, str) or not route:
            raise ValueError("scope and route must be non-empty strings")
        return self.contract_route_template.format(scope=scope, route=route)

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
    forms: ApplicationFormPort = NoApplicationForms()
    #: Scopes the adapter's goal data reads under (`goal.resolve.service.GoalData`), for an
    #: application whose goal-directed reads are not already covered by a tool's scope.
    goal_scopes: frozenset[str] = frozenset()
    operations: object = None

    def __post_init__(self) -> None:
        from .business_operations import BusinessOperationPort
        if self.operations is not None and not isinstance(self.operations, BusinessOperationPort):
            raise TypeError("operations must implement the optional BusinessOperationPort")
        if (not isinstance(self.goal_scopes, frozenset)
                or not all(isinstance(s, str) and s.strip() for s in self.goal_scopes)):
            raise TypeError("goal scopes must be a frozenset of non-empty strings")
        checks = (
            isinstance(self.identity, ApplicationIdentityPort),
            isinstance(self.routes, ApplicationRoutePort),
            isinstance(self.postconditions, BusinessPostconditionVerifier),
            isinstance(self.reconciliation, ApplicationMutationReconciler),
            isinstance(self.data, BusinessDataClassifier),
            isinstance(self.forms, ApplicationFormPort),
        )
        if not all(checks):
            raise TypeError("application ports do not implement the required contracts")
