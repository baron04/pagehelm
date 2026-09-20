"""Composition ports and top-level application adapter for Twenty."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.request import urlopen

from web_operation_agent.business.mutation import MutationJournal
from web_operation_agent.business.runtime import BusinessSkill, BusinessToolRuntime
from web_operation_agent.composition.descriptor import (
    APPLICATION_ADAPTER_VERSION,
    ApplicationDescriptor,
)
from web_operation_agent.composition.ports import (
    ApplicationPorts,
    DeclaredDataClassifier,
    DeclaredRoutePort,
    MutationReconciliation,
    ResultPostconditionVerifier,
)
from web_operation_agent.state.identity import (IdentityAssertion, PrincipalContext,
                                                PrincipalError, bind_matching_identity)

from .executor import TwentyBusinessToolExecutor
from .identity import TwentyOAuthTokenStore, TwentyPageSessionIdentityResolver
from .specs import (_MUTATION_SKILL_RESOURCE, _OPPORTUNITIES_SKILL_RESOURCE,
                    _PEOPLE_SKILL_RESOURCE, _QUERY_SKILL_RESOURCE, MUTATION_SPECS,
                    READ_SPECS, _packaged_skill, _validated_origin,
                    normalize_twenty_domain)


class _TwentyIdentityPort:
    required = True

    def __init__(self, executor: TwentyBusinessToolExecutor,
                 required_scopes: frozenset[str]):
        self._executor = executor
        self.required_scopes = required_scopes

    def bind(self, page_identity: IdentityAssertion, *, delegation_id: str,
             lifetime_s: int = 300, now_ms: int | None = None) -> PrincipalContext:
        delegated = self._executor.resolve_delegated_identity(
            lifetime_s=lifetime_s, now_ms=now_ms)
        return bind_matching_identity(
            page_identity, delegated, delegation_id=delegation_id,
            scopes=tuple(sorted(self.required_scopes)), now_ms=now_ms)

    def validate(self, principal: PrincipalContext | None, *,
                 application_id: str) -> None:
        if principal is None:
            raise PrincipalError(
                "Twenty requires a verified application principal")
        principal.require_live(application_id=application_id)


class _TwentyMutationReconciler:
    def __init__(self, executor, enabled):
        self._executor = executor
        self.supported_tools = frozenset({"create_company"}) if enabled else frozenset()

    def reconcile(self, name, business_key, *, evidence, principal):
        if name not in self.supported_tools or principal is None:
            raise ValueError("reconciliation requires an enabled tool and principal")
        principal.require_live(application_id="twenty")
        if "companies:read" not in principal.scopes:
            raise PrincipalError("reconciliation requires companies:read scope")
        delegated = self._executor.resolve_delegated_identity()
        if any(getattr(principal, field) != getattr(delegated, field)
               for field in ("tenant_id", "workspace_id", "actor_id")):
            raise PrincipalError("reconciliation delegated identity changed")
        args = self._executor._normalize_company(business_key)
        payload, _status, _refreshed = self._executor._get_companies()
        rows = (payload.get("data") or {}).get("companies", [])
        complete = (payload.get("_woaPagination", {}).get("complete") is True
                    and payload.get("totalCount") == len(rows))
        matches = [row for row in rows if isinstance(row, dict)
                   and str(row.get("name", "")).strip().casefold() == args["name"].casefold()
                   and normalize_twenty_domain((row.get("domainName") or {}).get("primaryLinkUrl")
                       or (row.get("domainName") or {}).get("primaryLinkLabel")) == args["domain"]]
        # A matching row is not proof that this proposal created it. Absence also cannot
        # rule out an upstream write still in flight. Keep the journal unresolved.
        return MutationReconciliation(
            outcome="unknown", requires_human=True,
            reason=(f"Current matching records: {len(matches)}; complete scan: {complete}. "
                    "Establish the original writer's final outcome before resolving."))


class TwentyApplicationAdapter:
    """Application-owned facts and tools for the Twenty Companies integration."""

    def __init__(self, *, base_url: str, token_path: str | Path,
                 registration_path: str | Path,
                 skill_path: str | Path | None = None,
                 mutation_skill_path: str | Path | None = None,
                 allow_mutations: bool = False,
                 mutation_journal: MutationJournal | None = None):
        if not isinstance(allow_mutations, bool):
            raise ValueError("allow_mutations must be a boolean")
        if mutation_skill_path is not None and not allow_mutations:
            raise ValueError("mutation_skill_path requires allow_mutations=True")
        if allow_mutations and mutation_journal is None:
            raise ValueError(
                "allow_mutations=True requires an explicit mutation journal")
        self.origin = _validated_origin(base_url)
        self.page_controller_module = self.origin + "/woa/page-controller.js"
        executor = TwentyBusinessToolExecutor(
            base_url=self.origin,
            tokens=TwentyOAuthTokenStore(token_path, registration_path),
            allow_mutations=allow_mutations,
        )
        query_skill = (BusinessSkill.load(skill_path) if skill_path is not None
                       else _packaged_skill(_QUERY_SKILL_RESOURCE))
        skills = [query_skill, _packaged_skill(_PEOPLE_SKILL_RESOURCE),
                  _packaged_skill(_OPPORTUNITIES_SKILL_RESOURCE)]
        if allow_mutations:
            skills.append(
                BusinessSkill.load(mutation_skill_path)
                if mutation_skill_path is not None
                else _packaged_skill(_MUTATION_SKILL_RESOURCE))
        required_scopes = {spec.name: spec.scope for spec in READ_SPECS}
        if allow_mutations:
            required_scopes.update({spec.name: spec.scope for spec in MUTATION_SPECS})
        specs = (*READ_SPECS, *(MUTATION_SPECS if allow_mutations else ()))
        data = DeclaredDataClassifier(tuple(
            (spec.name, spec.data_types) for spec in sorted(specs, key=lambda spec: spec.name)))
        postconditions = ResultPostconditionVerifier(
            tuple((spec.name, ("content.readback_verified", "audit.readback_verified"))
                  for spec in MUTATION_SPECS) if allow_mutations else ())
        routes = DeclaredRoutePort(
            application_id="twenty",
            tool_route_patterns=(
                r"/objects/(companies|people|opportunities)",
                r"/object/(company|person|opportunity)/[0-9a-f-]{36}"),
        )
        self.business_tools = BusinessToolRuntime(
            tuple(skills), executor, application_id="twenty",
            required_scopes=required_scopes,
            mutation_journal=mutation_journal,
            data_classifier=data,
            postcondition_verifier=postconditions,
            route_validator=routes)
        self._executor = executor
        identity = _TwentyIdentityPort(
            executor, frozenset(required_scopes.values()))
        self.ports = ApplicationPorts(
            identity=identity,
            routes=routes,
            postconditions=postconditions,
            reconciliation=_TwentyMutationReconciler(executor, allow_mutations),
            data=data,
        )
        self.descriptor = ApplicationDescriptor(
            schema_version=APPLICATION_ADAPTER_VERSION,
            application_id="twenty",
            display_name="Twenty CRM",
            origin=self.origin,
            navigation_mode="spa",
            page_controller_module=self.page_controller_module,
            catalog_sha256=self.business_tools.catalog_sha256,
        )

    def health(self) -> dict:
        try:
            with urlopen(self.origin + "/healthz", timeout=5) as response:
                payload = json.load(response)
                return {"passed": response.status == 200 and payload.get("status") == "ok",
                        "http_status": response.status}
        except Exception:
            return {"passed": False, "http_status": 0}

    def bind_principal(self, page_identity: IdentityAssertion, *,
                       delegation_id: str, lifetime_s: int = 300,
                       now_ms: int | None = None) -> PrincipalContext:
        """Match a trusted Twenty page-session assertion to the delegated OAuth actor.

        The application host must obtain ``page_identity`` from Twenty's authenticated
        server session. DOM text or a browser-provided user id is not trusted evidence.
        """
        return self.ports.identity.bind(
            page_identity, delegation_id=delegation_id,
            lifetime_s=lifetime_s, now_ms=now_ms)

    def bind_browser_session(self, storage_state_path: str | Path, *,
                             delegation_id: str, lifetime_s: int = 300,
                             now_ms: int | None = None) -> PrincipalContext:
        """Bind the exact browser storage state to the delegated OAuth identity."""
        page = TwentyPageSessionIdentityResolver(
            base_url=self.origin,
            storage_state_path=storage_state_path,
            timeout_s=self._executor.timeout_s,
        ).resolve(lifetime_s=lifetime_s, now_ms=now_ms)
        return self.bind_principal(
            page, delegation_id=delegation_id,
            lifetime_s=lifetime_s, now_ms=now_ms)

    def page_session_binding(self, storage_state: str | Path | dict, *,
                             now_ms: int | None = None) -> str:
        """Hash the active Twenty session cookie without exposing it to the Agent."""
        source = ({"storage_state": storage_state}
                  if isinstance(storage_state, dict)
                  else {"storage_state_path": storage_state})
        return TwentyPageSessionIdentityResolver(
            base_url=self.origin,
            timeout_s=self._executor.timeout_s,
            **source,
        ).binding_digest(now_ms=now_ms)
