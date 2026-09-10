"""Apache OFBiz route semantics for the optional legacy source integration."""
from __future__ import annotations

from ..application import DeclaredRoutePort


OFBIZ_ROUTE_PORT = DeclaredRoutePort(
    application_id="apache.ofbiz",
    tool_route_patterns=(r"/[A-Za-z0-9_]+/control/[^?#;]+",),
    session_handoff_query_keys=frozenset({
        "externalloginkey", "externaloginkey", "externloginkey", "jsessionid",
    }),
    contract_route_pattern=(
        r"/(?P<scope>[A-Za-z0-9_]+)/control/(?P<route>[^?;#]+)"),
    application_entry_patterns=(r"/[A-Za-z0-9_]+/control/main",),
)


def ofbiz_route_identity(url: str) -> tuple[str, str]:
    """Expose the source-contract identity without teaching the core OFBiz URL syntax."""
    return OFBIZ_ROUTE_PORT.contract_route_identity(url)
