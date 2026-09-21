"""Typed Odoo reads, form preparation, and guarded JSON-RPC writes."""
from __future__ import annotations

import threading

from .identity import OdooApiKeyStore
from .generic_reads import (GENERIC_READ_SCHEMAS, GENERIC_READ_TOOLS,
                            OdooGenericReadTools)
from .mutations import OdooMutationTools
from .reads import OdooReadTools
from .specs import (COLLECTIONS, MUTATION_SPECS, MUTATION_TOOLS,
                    PREPARE_CUSTOMER_EDIT_SCHEMA, PREPARE_CUSTOMER_EDIT_TOOL,
                    PREPARE_CUSTOMER_FORM_SCHEMA, PREPARE_CUSTOMER_FORM_TOOL,
                    QUERY_SCHEMAS, _validated_origin)
from .transport import OdooRpcClient


class OdooBusinessToolExecutor(OdooGenericReadTools, OdooReadTools,
                               OdooMutationTools, OdooRpcClient):
    """Typed reads, transient form preparation and guarded writes over delegated JSON-RPC."""

    def __init__(self, *, base_url: str, keys: OdooApiKeyStore, timeout_s: float = 20,
                 allow_mutations: bool = False, allow_private_http: bool = False):
        if not isinstance(allow_mutations, bool):
            raise ValueError("allow_mutations must be a boolean")
        self.base_url = _validated_origin(
            base_url, allow_private_http=allow_private_http)
        self.keys = keys
        self.timeout_s = timeout_s
        self.allow_mutations = allow_mutations
        self._uid: int | None = None
        self._time_zone: str | None = None
        self._lock = threading.Lock()

    # -- tool catalog -------------------------------------------------------------------
    def tool_schemas(self) -> tuple[dict, ...]:
        reads = tuple(QUERY_SCHEMAS[collection.tool] for collection in COLLECTIONS)
        writes = tuple(spec.schema for spec in MUTATION_SPECS)
        return reads + GENERIC_READ_SCHEMAS + (PREPARE_CUSTOMER_FORM_SCHEMA,) + (
            (PREPARE_CUSTOMER_EDIT_SCHEMA,) + writes if self.allow_mutations else ())

    def tool_effects(self) -> dict[str, str]:
        effects = {collection.tool: "read" for collection in COLLECTIONS}
        effects.update({name: "read" for name in GENERIC_READ_TOOLS})
        effects[PREPARE_CUSTOMER_FORM_TOOL] = "read"
        if self.allow_mutations:
            # Preparing an edit reads the record and types into the page; the application,
            # not this tool, does the writing -- but it may only be offered where writes are.
            effects[PREPARE_CUSTOMER_EDIT_TOOL] = "read"
            effects.update({tool: "write" for tool in MUTATION_TOOLS})
        return effects

    def execute(self, name: str, args: dict, *, context=None):
        if name in GENERIC_READ_TOOLS:
            return self.execute_generic(name, args, context=context)
        return OdooReadTools.execute(self, name, args, context=context)
