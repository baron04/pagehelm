"""Typed Odoo reads, form preparation, and guarded JSON-RPC writes."""
from __future__ import annotations

import threading

from web_operation_agent.business.entities import (ENTITY_READ_SCHEMAS,
                                                   ENTITY_READ_TOOLS,
                                                   RESOLVE_RECORD_REFERENCE_SCHEMA,
                                                   RESOLVE_RECORD_REFERENCE_TOOL,
                                                   EntityReadTools, ReferenceLookupTools)
from web_operation_agent.business.page_entries import (
    FIND_PAGE_ENTRIES_SCHEMA, FIND_PAGE_ENTRIES_TOOL, PageEntryTools)

from .identity import OdooApiKeyStore
from .generic_reads import OdooEntityReadPort, OdooReferenceLookupPort
from .page_entries import OdooPageEntryPort
from .mutations import OdooMutationTools
from .reads import OdooReadTools
from .specs import (APPLICATION_ID, COLLECTIONS, MUTATION_SPECS, MUTATION_TOOLS,
                    PREPARE_CUSTOMER_EDIT_SCHEMA, PREPARE_CUSTOMER_EDIT_TOOL,
                    PREPARE_CUSTOMER_FORM_SCHEMA, PREPARE_CUSTOMER_FORM_TOOL,
                    QUERY_SCHEMAS, _validated_origin)
from .transport import OdooRpcClient


class OdooBusinessToolExecutor(OdooReadTools, OdooMutationTools, OdooRpcClient):
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
        self._entity_reads = EntityReadTools(
            OdooEntityReadPort(self), application_id=APPLICATION_ID)
        self._reference_lookup = ReferenceLookupTools(
            OdooEntityReadPort(self), OdooReferenceLookupPort(self),
            application_id=APPLICATION_ID)
        self._page_entries = PageEntryTools(
            OdooPageEntryPort(self), application_id=APPLICATION_ID)

    # -- tool catalog -------------------------------------------------------------------
    def tool_schemas(self) -> tuple[dict, ...]:
        reads = tuple(QUERY_SCHEMAS[collection.tool] for collection in COLLECTIONS)
        writes = tuple(spec.schema for spec in MUTATION_SPECS)
        return reads + ENTITY_READ_SCHEMAS + (RESOLVE_RECORD_REFERENCE_SCHEMA,
                                              FIND_PAGE_ENTRIES_SCHEMA,
                                              PREPARE_CUSTOMER_FORM_SCHEMA) + (
            (PREPARE_CUSTOMER_EDIT_SCHEMA,) + writes if self.allow_mutations else ())

    def tool_effects(self) -> dict[str, str]:
        effects = {collection.tool: "read" for collection in COLLECTIONS}
        effects.update({name: "read" for name in ENTITY_READ_TOOLS})
        effects[RESOLVE_RECORD_REFERENCE_TOOL] = "read"
        effects[FIND_PAGE_ENTRIES_TOOL] = "read"
        effects[PREPARE_CUSTOMER_FORM_TOOL] = "read"
        if self.allow_mutations:
            # Preparing an edit reads the record and types into the page; the application,
            # not this tool, does the writing -- but it may only be offered where writes are.
            effects[PREPARE_CUSTOMER_EDIT_TOOL] = "read"
            effects.update({tool: "write" for tool in MUTATION_TOOLS})
        return effects

    def execute(self, name: str, args: dict, *, context=None):
        if name in ENTITY_READ_TOOLS:
            return self._entity_reads.execute(name, args, context=context)
        if name == RESOLVE_RECORD_REFERENCE_TOOL:
            return self._reference_lookup.execute(args, context=context)
        if name == FIND_PAGE_ENTRIES_TOOL:
            return self._page_entries.execute(args, context=context)
        return OdooReadTools.execute(self, name, args, context=context)
