"""Declarative application adapter assembling identity, read, write, and route ports."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Callable, Mapping
import urllib.error
import urllib.request

from web_operation_agent.business.entities import (
    DESCRIBE_ENTITY_TOOL,
    DISCOVER_ENTITIES_TOOL,
    ENTITY_READ_TOOLS,
    QUERY_ENTITY_RECORDS_TOOL,
    RESOLVE_RECORD_REFERENCE_TOOL,
)
from web_operation_agent.business.forms import NoApplicationForms
from web_operation_agent.business.goal_candidates import RESOLVE_GOAL_CANDIDATES_TOOL
from web_operation_agent.business.page_entries import FIND_PAGE_ENTRIES_TOOL
from web_operation_agent.business.runtime import BusinessSkill, BusinessToolRuntime
from web_operation_agent.business.tools.contracts import ToolOperation
from web_operation_agent.composition.descriptor import APPLICATION_ADAPTER_VERSION, ApplicationDescriptor
from web_operation_agent.composition.ports import (
    ApplicationPorts,
    DeclaredDataClassifier,
    DeclaredRoutePort,
    ResultPostconditionVerifier,
)
from .declarative_forms import DeclarativeFormPort
from .declarative_identity import (
    DeclarativeIdentityPort,
    HttpWhoamiIdentityPort,
    IdentityResolver,
    ResolvedActor,
)
from .declarative_reads import (
    DeclarativeEntityReadPort,
    DeclarativePageEntryPort,
    DeclarativeReadExecutor,
    DeclarativeRestClient,
)
from .declarative_writes import (
    DeclarativeReconciler,
    StatusToggleSpec,
    TypedWriteSpec,
    build_declarative_operations,
)
from .openapi import EntitySpec, FormSpec, PageSpec


@dataclass(frozen=True, slots=True)
class DeclarativeAppSpec:
    """Full declarative specification of a PageHelm host integration."""

    application_id: str
    display_name: str
    entities: tuple[EntitySpec, ...]
    pages: tuple[PageSpec, ...]
    page_resolver: IdentityResolver | None = None
    delegated_resolver: Callable[[], ResolvedActor] | None = None
    same_origin_shared_session: bool = False
    forms: tuple[FormSpec, ...] = ()
    navigation_mode: str = "spa"
    page_controller_path: str = "/woa/page-controller.js"
    tool_route_patterns: tuple[str, ...] = ()
    session_handoff_query_keys: frozenset[str] = frozenset()
    skills_dir: Path | None = None
    legacy_skill_files: tuple[Path, ...] = ()
    legacy_write_skill_files: tuple[Path, ...] = ()
    write_specs: tuple[TypedWriteSpec, ...] = ()
    status_toggles: tuple[StatusToggleSpec, ...] = ()
    default_read_scope: str = ""
    default_write_scope: str = ""
    health_path: str = "/healthz"


class _DelegatedToolsAdapter:
    """Bridge read executor and registered write ToolRegistry for Skill-backed turns."""

    def __init__(self, reads, writes, write_names: frozenset[str]):
        self.reads, self.writes, self._write_names = reads, writes, write_names

    def tool_schemas(self):
        return (
            *[s for s in self.reads.tool_schemas() if s["function"]["name"] not in self._write_names],
            *self.writes.tool_schemas(),
        )

    def tool_effects(self):
        return {
            s["function"]["name"]: ("write" if s["function"]["name"] in self._write_names else "read")
            for s in self.tool_schemas()
        }

    def execute(self, name, args, *, context=None):
        return self.reads.execute(name, args, context=context)

    def prepare_mutation(self, name, args, *, context=None):
        return self.writes.prepare_mutation(name, args, context=context)

    def execute_mutation(self, name, args, *, context=None):
        return self.writes.execute_mutation(name, args, context=context)


class DeclarativeApplicationAdapter:
    """Standard application-neutral adapter driven by ``DeclarativeAppSpec``."""

    def __init__(
        self,
        spec: DeclarativeAppSpec,
        origin: str,
        *,
        api_token: str = "",
        headers_factory: Callable[[], Mapping[str, str]] | None = None,
        allow_mutations: bool = False,
        mutation_journal=None,
        extra_skills: tuple[BusinessSkill, ...] = (),
        standard_skills: bool = False,
        convention_updates: bool = False,
        menu_fetcher: Callable[[Any], Mapping[str, Any]] | None = None,
        scope_formatter: Callable[[Any], str] | None = None,
        executor_cls: type[DeclarativeReadExecutor] = DeclarativeReadExecutor,
        entity_port_cls: type[DeclarativeEntityReadPort] = DeclarativeEntityReadPort,
        form_port_cls: type[DeclarativeFormPort] = DeclarativeFormPort,
        identity_port_cls: Callable[..., Any] = DeclarativeIdentityPort,
        operations_factory: Callable[[Any, Any], Any] | None = None,
    ):
        if allow_mutations and mutation_journal is None:
            raise ValueError("allow_mutations=True requires an explicit mutation journal")
        if not isinstance(extra_skills, tuple) or any(
            not isinstance(s, BusinessSkill) or not s.read_only for s in extra_skills
        ):
            raise TypeError("extra synthetic Skills must be read-only artifacts")

        self.spec, self.origin = spec, origin.rstrip("/")
        self.client = DeclarativeRestClient(self.origin, api_token=api_token, headers_factory=headers_factory)
        executor = executor_cls(
            application_id=spec.application_id, client=self.client, entities=spec.entities,
            pages=spec.pages, menu_fetcher=menu_fetcher, write_specs=spec.write_specs,
            allow_mutations=allow_mutations, scope_formatter=scope_formatter, entity_port_cls=entity_port_cls,
        )
        self._executor = executor
        forms_port: Any = form_port_cls(spec.forms, executor=executor) if spec.forms else NoApplicationForms()

        read_scope = spec.default_read_scope or next((e.read_scope for e in spec.entities if e.read_scope), f"{spec.application_id}:read")
        write_scope = spec.default_write_scope or next((e.write_scope for e in spec.entities if e.write_scope), f"{spec.application_id}:write")
        self.read_scope, self.write_scope, self.allow_mutations = read_scope, write_scope, bool(allow_mutations)

        base_scopes: dict[str, str] = {
            **{e.list_tool_name: (e.read_scope or read_scope) for e in spec.entities if e.list_tool_name},
            **{name: read_scope for name in ENTITY_READ_TOOLS},
            RESOLVE_RECORD_REFERENCE_TOOL: read_scope,
            FIND_PAGE_ENTRIES_TOOL: read_scope,
            RESOLVE_GOAL_CANDIDATES_TOOL: read_scope,
        }
        if allow_mutations:
            for w in spec.write_specs:
                base_scopes[w.name] = w.required_scope

        extra_scopes = {write_scope} if (allow_mutations and convention_updates and write_scope) else frozenset()
        self.identity = identity_port_cls(
            application_id=spec.application_id,
            page_resolver=spec.page_resolver,
            delegated_resolver=spec.delegated_resolver,
            has_delegated_credential=bool(api_token or headers_factory is not None),
            same_origin_shared_session=spec.same_origin_shared_session,
            required_scopes=frozenset(base_scopes.values()) | forms_port.required_scopes | extra_scopes,
        )
        executor.set_identity_port(self.identity)

        operations = None
        skills: list[BusinessSkill] = []
        if standard_skills:
            if extra_skills:
                raise ValueError("standard packages use their independent installed registry")
            if operations_factory is not None:
                operations = operations_factory(executor, self.identity)
            else:
                operations = build_declarative_operations(
                    namespace=spec.application_id, application_id=spec.application_id,
                    skills_dir=spec.skills_dir, executor=executor, enabled=allow_mutations,
                    write_specs=spec.write_specs, entities=spec.entities, status_toggles=spec.status_toggles,
                    convention_updates=convention_updates, identity_port=self.identity, write_scope=write_scope,
                )
        else:
            skills.extend(BusinessSkill.from_bytes(p.read_bytes(), artifact_uri=f"file://{p.name}") for p in spec.legacy_skill_files)
            if allow_mutations:
                skills.extend(BusinessSkill.from_bytes(p.read_bytes(), artifact_uri=f"file://{p.name}") for p in spec.legacy_write_skill_files)
            skills.extend(extra_skills)

        declarations: list[tuple[str, tuple[str, ...]]] = [
            (e.list_tool_name, (f"application.{e.name}", "business.identifier", "application.route"))
            for e in spec.entities if e.list_tool_name
        ] + [
            (DISCOVER_ENTITIES_TOOL, ("application.metadata",)),
            (DESCRIBE_ENTITY_TOOL, ("application.metadata",)),
            (QUERY_ENTITY_RECORDS_TOOL, ("business.generic_record", "business.identifier")),
            (RESOLVE_RECORD_REFERENCE_TOOL, ("business.generic_record", "business.identifier", "application.route")),
            (FIND_PAGE_ENTRIES_TOOL, ("application.metadata", "application.route")),
            (RESOLVE_GOAL_CANDIDATES_TOOL, ("application.metadata", "application.route", "business.identifier")),
        ]
        post_rules: list[tuple[str, tuple[str, ...]]] = []
        extra_reconcile: set[str] = set()
        if allow_mutations:
            for w in spec.write_specs:
                declarations.append((w.name, w.data_classes))
                post_rules.append((w.name, w.postcondition_paths))
            if operations is not None:
                declared_names = {name for name, _ in declarations}
                post_names = {name for name, _ in post_rules}
                for d in operations.tools.definitions:
                    if d.effect == "write":
                        base_scopes.setdefault(d.name, d.scope)
                        extra_reconcile.add(d.name)
                        if d.name not in declared_names:
                            declarations.append((d.name, ("application.record", "business.identifier", "business.mutation_result")))
                            declared_names.add(d.name)
                        if d.name not in post_names:
                            post_rules.append((d.name, ("content.verified", "audit.verified")))
                            post_names.add(d.name)

        data = DeclaredDataClassifier(tuple(sorted(declarations)))
        postconditions = ResultPostconditionVerifier(tuple(post_rules))
        routes = DeclaredRoutePort(
            application_id=spec.application_id,
            tool_route_patterns=spec.tool_route_patterns,
            session_handoff_query_keys=spec.session_handoff_query_keys,
        )

        if operations is None:
            self.business_tools = BusinessToolRuntime(
                tuple(skills), executor, application_id=spec.application_id, required_scopes=base_scopes,
                mutation_journal=mutation_journal, data_classifier=data, postcondition_verifier=postconditions, route_validator=routes,
            )
        else:
            write_names = frozenset(d.name for d in operations.tools.definitions) | frozenset(w.name for w in spec.write_specs)
            op_map = {**{name: ToolOperation(spec.entities[0].name, "read") for name in base_scopes if name not in write_names}, **{d.name: d.operation for d in operations.tools.definitions}}
            self.business_tools = BusinessToolRuntime(
                (), _DelegatedToolsAdapter(executor, operations.tools, write_names),
                application_id=spec.application_id, required_scopes=base_scopes, mutation_journal=mutation_journal,
                data_classifier=data, postcondition_verifier=postconditions, route_validator=routes,
                skill_registry=operations.skills, require_skill_for_writes=True, operations=op_map,
            )

        reconcile_tools = (
            frozenset(w.name for w in spec.write_specs) | extra_reconcile
        ) if allow_mutations else frozenset()
        reconciler = DeclarativeReconciler(
            application_id=spec.application_id, read_scope=read_scope, executor=executor,
            supported_tools=reconcile_tools, write_specs={w.name: w for w in spec.write_specs},
        )
        self.ports = ApplicationPorts(
            identity=self.identity, routes=routes, postconditions=postconditions,
            reconciliation=reconciler, data=data, forms=forms_port, operations=operations,
        )
        self.descriptor = ApplicationDescriptor(
            schema_version=APPLICATION_ADAPTER_VERSION, application_id=spec.application_id,
            display_name=spec.display_name, origin=self.origin, navigation_mode=spec.navigation_mode,
            page_controller_module=self.origin + spec.page_controller_path,
            catalog_sha256=self.business_tools.catalog_sha256,
        )

    def get(self, path: str, **params) -> dict:
        """One read under the delegated actor."""
        return self._executor.get(path, **params)

    def health(self) -> dict:
        try:
            with urllib.request.urlopen(self.descriptor.origin + self.spec.health_path, timeout=2) as response:
                passed = response.status == 200 and json.load(response) == {"ready": True}
        except (OSError, ValueError, urllib.error.URLError):
            passed = False
        return {"passed": passed, "application_id": self.descriptor.application_id}


__all__ = [
    "DeclarativeAppSpec",
    "DeclarativeApplicationAdapter",
    "DeclarativeEntityReadPort",
    "DeclarativeIdentityPort",
    "DeclarativePageEntryPort",
    "DeclarativeReadExecutor",
    "DeclarativeRestClient",
    "HttpWhoamiIdentityPort",
    "IdentityResolver",
    "ResolvedActor",
]
