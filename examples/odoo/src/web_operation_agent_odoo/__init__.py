"""Odoo 18 reference integration for web-operation-agent.

This distribution owns everything Odoo-specific: the web-client route grammar, the
compiler that turns Odoo's own module XML and model Python into a cited ``SourceMap``, and
the application adapter that implements the ``ApplicationPorts`` against Odoo's
delegated JSON-RPC API. The core package imports none of it.
"""
from .route import (ODOO_ROUTE_PORT, ODOO_SESSION_HANDOFF_KEYS, ODOO_SOURCE_SCOPE,
                    odoo_route_identity)
from .sourcemap import ODOO_SOURCE_POLICY, OdooCompiler

__version__ = "0.2.0"

__all__ = [
    "ODOO_ROUTE_PORT",
    "ODOO_SESSION_HANDOFF_KEYS",
    "ODOO_SOURCE_POLICY",
    "ODOO_SOURCE_SCOPE",
    "OdooCompiler",
    "odoo_route_identity",
]
