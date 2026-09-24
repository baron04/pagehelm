"""Odoo 18 web-client route semantics.

Odoo serves its whole back office from one server route, ``/odoo/<path:subpath>``, and
resolves the subpath in the browser. Two subpath shapes carry meaning:

    /odoo/<action-path>            a collection, named by ``ir.actions.act_window.path``
    /odoo/<action-path>/<id>       one record inside that action
    /odoo/<model.name>/<id>        one record addressed by its model, the generic fallback

The action path is not a convention this file invented: it is a stored field, and the G2
spike compared all 32 of them against the compiled source with no mismatch. That is why
the source-contract identity of an Odoo page is ``(odoo, <action-path>)`` -- Odoo has a
single web client, so the scope is constant, and the
route is the part that actually varies.
"""
from __future__ import annotations

from web_operation_agent.composition.ports import DeclaredRoutePort


#: Odoo 18 keeps the session in an HttpOnly cookie and accepts no header or query carrier
#: (``odoo/http.py`` reads ``cookies.get('session_id')`` only). Older releases did put
#: ``session_id`` in URLs, and portal links still carry ``access_token``; both are listed so
#: a route carrying one is refused and a value reaching an audit record is redacted.
ODOO_SESSION_HANDOFF_KEYS = frozenset({"session_id", "access_token", "token"})

ODOO_ROUTE_PORT = DeclaredRoutePort(
    application_id="odoo",
    tool_route_patterns=(
        r"/odoo/[a-z][a-z0-9-]*",
        # Window actions without a custom path use their deployment XML ID in the
        # web-client URL. The page directory must be able to return these reviewed
        # destinations through the same typed-tool route contract.
        r"/odoo/action-[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*",
        r"/odoo/[a-z][a-z0-9-]*(?:/[a-z][a-z0-9-]*)*/new",
        r"/odoo/[a-z][a-z0-9-]*/[1-9][0-9]{0,9}",
        r"/odoo/[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+/[1-9][0-9]{0,9}",
    ),
    session_handoff_query_keys=ODOO_SESSION_HANDOFF_KEYS,
    contract_route_pattern=r"/(?P<scope>odoo)/(?P<route>[a-z][a-z0-9_.-]*)",
    contract_route_template="/{scope}/{route}",
)

#: The one scope every Odoo route lives in. A source-armed session is served this set.
ODOO_SOURCE_SCOPE = "odoo"


def odoo_route_identity(url: str) -> tuple[str, str]:
    """Expose the source-contract identity without teaching the core Odoo's URL syntax."""
    return ODOO_ROUTE_PORT.contract_route_identity(url)
