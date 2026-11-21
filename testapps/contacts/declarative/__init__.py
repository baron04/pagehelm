"""Declarative application adapter prototype extracted for reference integrations."""
from .declarative import (
    DeclarativeAppSpec,
    DeclarativeApplicationAdapter,
    DeclarativeIdentityPort,
    HttpWhoamiIdentityPort,
    IdentityResolver,
    ResolvedActor,
)
from .declarative_loop import (
    DeclarativeGoalPort,
    DeclarativeModelGoalPort,
    DeclarativeProcedure,
    build_declarative_profile,
)
from .declarative_writes import StatusToggleSpec, SubmitSpec, TypedWriteSpec
from .openapi import (
    EntitySpec,
    FRONTEND_PRESETS,
    FieldSpec,
    FormSpec,
    FrontendPreset,
    PageSpec,
    QueryFormSpec,
    entities_from_openapi,
)

__all__ = [
    "DeclarativeAppSpec",
    "DeclarativeApplicationAdapter",
    "DeclarativeGoalPort",
    "DeclarativeIdentityPort",
    "DeclarativeModelGoalPort",
    "DeclarativeProcedure",
    "EntitySpec",
    "FRONTEND_PRESETS",
    "FieldSpec",
    "FormSpec",
    "FrontendPreset",
    "HttpWhoamiIdentityPort",
    "IdentityResolver",
    "PageSpec",
    "QueryFormSpec",
    "ResolvedActor",
    "StatusToggleSpec",
    "SubmitSpec",
    "TypedWriteSpec",
    "build_declarative_profile",
    "entities_from_openapi",
]
