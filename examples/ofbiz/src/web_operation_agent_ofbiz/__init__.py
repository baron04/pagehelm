"""Apache OFBiz distribution: the route grammar, source compiler and App Skill provider
that used to live inside the core package.

The core Agent has no OFBiz-specific code; everything here is a concrete implementation of
core's ports (``ApplicationRoutePort``, ``SourceCompiler``, ``ApplicationKnowledgePort``).
"""
from __future__ import annotations

__version__ = "0.1.0"

from .knowledge import AppSkill, KnowledgeRouter
from .route import OFBIZ_ROUTE_PORT, ofbiz_route_identity
from .sourcemap import OFBizCompiler

__all__ = [
    "AppSkill",
    "KnowledgeRouter",
    "OFBIZ_ROUTE_PORT",
    "ofbiz_route_identity",
    "OFBizCompiler",
]
