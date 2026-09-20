"""A synthetic application route grammar for core tests.

Before this existed the core suite imported a reference adapter's route module, which had two
costs. The suite could not run without an application distribution installed, and -- worse --
every generic consumer of a route port (`affordance`, `contract`, `grounding`, `source_skill`,
`session`) was only ever exercised against one application's URL spelling, so a
grammar assumption leaking into the core would have passed unnoticed.

The spelling here is deliberately neither OFBiz's nor Odoo's: the contract segment is `do`,
not `control`, and it is not a fixed prefix like Odoo's `/odoo/`. A core module that still
works against all three is not encoding any one application's syntax.
"""
from __future__ import annotations

from web_operation_agent.composition.ports import DeclaredRoutePort


#: The one synthetic application every core route test speaks about.
FIXTURE_APPLICATION_ID = "fixture.app"
FIXTURE_SEGMENT = "do"

FIXTURE_ROUTE_PORT = DeclaredRoutePort(
    application_id=FIXTURE_APPLICATION_ID,
    tool_route_patterns=(rf"/[A-Za-z0-9_]+/{FIXTURE_SEGMENT}/[^?#;]+",),
    session_handoff_query_keys=frozenset({"fixturesessionkey"}),
    contract_route_pattern=(
        rf"/(?P<scope>[A-Za-z0-9_]+)/{FIXTURE_SEGMENT}/(?P<route>[^?;#]+)"),
    contract_route_template=f"/{{scope}}/{FIXTURE_SEGMENT}/{{route}}",
    application_entry_patterns=(rf"/[A-Za-z0-9_]+/{FIXTURE_SEGMENT}/main",),
)


def fixture_route_identity(url: str) -> tuple[str, str]:
    """The `(scope, route)` identity of one synthetic-application URL."""
    return FIXTURE_ROUTE_PORT.contract_route_identity(url)


def fixture_route(scope: str, route: str) -> str:
    """Compose one synthetic-application route, the inverse of `fixture_route_identity`."""
    return FIXTURE_ROUTE_PORT.compose_contract_route(scope, route)
