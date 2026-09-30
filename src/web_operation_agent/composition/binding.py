"""Binding an adapter to a release, once, before any user task reaches the Agent loop.

``bind_application`` is the single checkpoint that says a descriptor, a tool catalog and
the application ports describe the same application; the generic composition consumes adapters
through ``ApplicationAdapter`` and receives a ``BoundApplication``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..business.runtime import BusinessToolRuntime
from .descriptor import ApplicationDescriptor
from .ports import ApplicationPorts


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
    declared_scopes = frozenset(scope for scope in tools.required_scopes.values() if scope) \
        | ports.forms.required_scopes | ports.goal_scopes
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
