"""Actor-bound Yudao reads behind the existing application-neutral tool contracts.

The adapter translates metadata and API access, not natural-language questions. The
legacy goal port remains available to the frozen evaluation apparatus; production turns
may combine generic reads, repository evidence and fresh page observations in one loop.
"""
from __future__ import annotations

import time
from datetime import date
from pathlib import Path
from typing import Callable

from web_operation_agent.business.runtime import BusinessToolRuntime
from web_operation_agent.business.tools.contracts import ToolOperation
from web_operation_agent.business.forms import ApplicationFormPort, NoApplicationForms
from web_operation_agent.composition.descriptor import (APPLICATION_ADAPTER_VERSION,
                                                        ApplicationDescriptor)
from web_operation_agent.composition.ports import (ApplicationPorts, ConservativeMutationReconciler,
                                                   DeclaredDataClassifier, DeclaredRoutePort,
                                                   ResultPostconditionVerifier)
from web_operation_agent.goal.conventions import load_conventions
from web_operation_agent.goal.resolve.service import DataGoalPort
from web_operation_agent.goal.read_tools import DataReadTools
from web_operation_agent.state.identity import (PRINCIPAL_CONTEXT_VERSION, IdentityAssertion,
                                                PrincipalContext, PrincipalError,
                                                bind_matching_identity)

from .client import YudaoAccount, YudaoClient, YudaoReadRefused
from .conventions import REFERENCE_CONVENTIONS
from .goal import YudaoGoalData
from .generic_reads import YudaoReadTools
from .bound_data_reads import BoundDataReads
from .ui_source import UiSource

APPLICATION_ID = "yudao"
#: Where the application's own pages live: every admin page is a path of words.
_PAGE_ROUTES = (r"/[a-z0-9][a-z0-9/_-]*",)
#: Query keys that would carry a session into a page address; refused and redacted.
SESSION_HANDOFF_KEYS = frozenset({"token", "access_token", "accessToken", "refresh_token"})
#: Where the deployment serves the pinned page controller, same-origin.
PAGE_CONTROLLER_PATH = "/woa/page-controller.js"
NATIVE_PAGE_CONTROLLER_PATH = "/woa/yudao-page-controller.js"


class YudaoIdentityPort:
    """The browser's token and the server's account, each asked of yudao who it is.

    `get-permission-info` answers with the user's id; the tenant is the one both requests
    carry. `bind_matching_identity` refuses unless both name the same user in the same
    tenant, which keeps the server from reading as anyone but the person at the page.
    """

    required = True
    required_scopes = frozenset({"yudao.read"})

    def __init__(self, base_url: str, client: YudaoClient, *, lifetime_s: int = 300, native_writes=False):
        self._base_url, self._client, self.lifetime_s = base_url.rstrip("/"), client, lifetime_s
        self.required_scopes = frozenset({"yudao.read", *(('yudao.write',) if native_writes else ())})

    def _assertion(self, user: dict, source: str) -> IdentityAssertion:
        issued = int(time.time() * 1000)
        return IdentityAssertion(
            schema_version=PRINCIPAL_CONTEXT_VERSION, source=source, application_id=APPLICATION_ID,
            tenant_id=str(self._client.tenant), workspace_id=str(user.get("deptId") or "none"),
            actor_id=str(user["id"]), issued_at=issued, expires_at=issued + self.lifetime_s * 1000)

    def page_identity(self, access_token: str) -> IdentityAssertion:
        """What the browser is, asked of yudao with the browser's own access token."""
        try:
            data = YudaoClient.for_token(self._base_url, access_token, self._client.tenant,
                                         timeout_s=10).get("system/auth/get-permission-info")
        except YudaoReadRefused as exc:
            if exc.code != 401:
                raise
            raise PrincipalError("the browser's session is not signed in to yudao") from exc
        if not isinstance(data, dict) or not isinstance(data.get("user"), dict):
            raise PrincipalError("the browser's session is not signed in to yudao")
        return self._assertion(data["user"], "page_session")

    def bind(self, page_identity: IdentityAssertion, *, delegation_id: str,
             lifetime_s: int = 300, now_ms: int | None = None) -> PrincipalContext:
        user = (self._client.get("system/auth/get-permission-info") or {}).get("user")
        if not isinstance(user, dict):
            raise PrincipalError("the server's yudao account has no user")
        return bind_matching_identity(page_identity, self._assertion(user, "delegated_api"),
                                      delegation_id=delegation_id,
                                      scopes=tuple(sorted(self.required_scopes)), now_ms=now_ms)

    def validate(self, principal: PrincipalContext | None, *, application_id: str) -> None:
        if principal is None:
            raise PrincipalError("this application requires a verified principal")
        principal.require_live(application_id=application_id)


class YudaoApplication:
    """The composed read-only adapter: `bind_application(YudaoApplication(...))`."""

    def __init__(self, origin: str, account: YudaoAccount | None, api_docs: dict | Callable[[], dict], *,
                 api_base_url: str = "", ui: UiSource | None = None,
                 time_zone: str = "Asia/Shanghai", today: date | None = None, source=None,
                 conventions: Path | None = REFERENCE_CONVENTIONS, client: YudaoClient | None = None,
                 native_tabs: bool = False, native_navigation: bool = False, page_write_guard: str = "",
                 forms: ApplicationFormPort | None = None, native_writes: bool = False,
                 mutation_journal=None, native_write_preparation: bool = False,
                 writing_gets: frozenset[str] = frozenset()):
        if type(native_write_preparation) is not bool:
            raise TypeError("native_write_preparation must be boolean")
        if native_write_preparation and native_writes:
            raise ValueError("preparation and execution modes are mutually exclusive")
        if not isinstance(native_writes, bool):
            raise TypeError("native_writes must be explicitly enabled")
        if not isinstance(native_tabs, bool) or (native_tabs and not page_write_guard):
            raise ValueError("native tabs require an explicitly configured page write guard")
        if not isinstance(native_navigation, bool) or native_navigation and not (native_tabs and page_write_guard):
            raise ValueError("native navigation requires native page observation and a write guard")
        if forms is not None and not page_write_guard:
            raise ValueError("native forms require an explicitly configured page write guard")
        self.origin = origin.rstrip("/")
        api_base = (api_base_url or self.origin).rstrip("/")
        # A client of its own account, or the one given (a person's own token).
        self.client = client if client is not None else YudaoClient(api_base, account)
        # The data port's own reads of application metadata go out four at a time, as its
        # lookups by name do (`reference_workers` below).
        self.goal_data = YudaoGoalData(self.client, api_docs, ui=ui, time_zone=time_zone,
                                      native_sections=native_tabs, read_workers=8,
                                      writing_gets=writing_gets)
        from .native_tabs import NativeTabPolicy
        self.native_tab_policy = NativeTabPolicy((ui or UiSource.none()).records.values()) if native_tabs else None
        if native_navigation:
            from .native_tabs import NativeNavigationPolicy
            self.native_tab_policy = NativeNavigationPolicy(self.origin, self.goal_data.index, self.native_tab_policy)
        self.read_only_tab_policy = self.native_tab_policy
        if source is not None:
            # The deployment's source rules: when a control shows, what an operation refuses.
            self.goal_data.attach_source(source)
        if conventions is not None:
            # What this organisation means by some words, as its owner reviewed them.
            self.goal_data.attach_conventions(load_conventions(conventions))
        from .page_goals import NativePageGoalPort
        goal_port = NativePageGoalPort if native_tabs else DataGoalPort
        self.goal_port = goal_port(self.goal_data, today=today, reference_workers=8)
        from .live_forms import LiveFormPort
        if isinstance(forms, LiveFormPort):
            from .live_goals import LiveFormsGoalPort
            self.goal_port = LiveFormsGoalPort(self.goal_data, forms, today=today, reference_workers=8)
            self.native_tab_policy = forms.click_policy(self.native_tab_policy)
        from .form_entries import NativeEntryFormPort
        if isinstance(forms, NativeEntryFormPort):
            from .form_goals import NativeFormsGoalPort
            self.goal_port = NativeFormsGoalPort(self.goal_data, forms.goal_source, entries=forms.goal_entries, updates=forms.goal_updates,
                                                 current_form=forms.current_goal_form,
                                                 today=today, reference_workers=8)
            self.native_tab_policy = forms.click_policy(self.native_tab_policy)
        if isinstance(self.goal_port, NativePageGoalPort):
            self.goal_port.allow_updates = bool(native_writes or native_write_preparation)
        reads = YudaoReadTools(self.goal_data, application_id=APPLICATION_ID)
        self.data_reads = DataReadTools(
            lambda context, checkpoint: BoundDataReads(self.goal_data, context, checkpoint),
            APPLICATION_ID, "yudao.read", today=today)
        names = tuple(reads.tool_effects())
        classes = {
            "discover_entities": ("application.metadata",),
            "describe_entity": ("application.metadata",),
            "query_entity_records": ("business.generic_record", "business.identifier"),
            "resolve_record_reference": ("business.generic_record", "business.identifier", "application.route"),
            "find_page_entries": ("application.metadata", "application.route"),
            "resolve_goal_candidates": ("application.metadata", "application.route", "business.identifier"),
        }
        from .writes import native_tools, NativePostconditions, CombinedTools
        from .skill_packages import packaged_skills
        from web_operation_agent.composition.business_operations import RegisteredOperations
        from web_operation_agent.business.tools.registry import ToolRegistry
        reviewed = native_tools(self.client, self.goal_data)
        skills = packaged_skills()
        if any(op not in {t.name for t in reviewed.definitions} for skill in skills.artifacts for op in skill.write_operations):
            raise ValueError("Skill references an unreviewed native operation")
        registry = native_tools(self.client, self.goal_data, runtime_dictionary=True,
            mutation_dispatch_enabled=not native_write_preparation) if (native_writes or native_write_preparation) else ToolRegistry(())
        mutation_names = frozenset(t.name for t in registry.definitions)
        data = DeclaredDataClassifier(tuple((name, classes[name]) for name in names) +
            tuple((name, ("business.generic_record", "business.identifier")) for name in sorted(mutation_names)))
        postconditions = NativePostconditions(mutation_names)
        routes = DeclaredRoutePort(APPLICATION_ID, _PAGE_ROUTES,
                                   session_handoff_query_keys=SESSION_HANDOFF_KEYS)
        self.identity = YudaoIdentityPort(api_base, self.client, native_writes=native_writes or native_write_preparation)
        self.business_tools = BusinessToolRuntime(
            (), CombinedTools(reads, registry) if registry else reads, application_id=APPLICATION_ID,
            required_scopes={**{name: "yudao.read" for name in names}, **{name: "yudao.write" for name in mutation_names}}, data_classifier=data,
            operations={**{name: ToolOperation("application", "read") for name in names},
                        **{t.name: t.operation for t in registry.definitions}},
            skill_registry=skills, require_skill_for_writes=True, mutation_journal=mutation_journal,
            mutation_dispatch_enabled=not native_write_preparation,
            postcondition_verifier=postconditions, route_validator=routes)
        self.ports = ApplicationPorts(
            identity=self.identity, routes=routes, postconditions=postconditions,
            reconciliation=ConservativeMutationReconciler(mutation_names), data=data,
            operations=RegisteredOperations(registry, skills, self.goal_data.invalidate_reads,
                observed_mutation_operations=frozenset(d.name for d in registry.definitions)),
            goal_scopes=YudaoIdentityPort.required_scopes,
            forms=forms if forms is not None else NoApplicationForms())
        self.descriptor = ApplicationDescriptor(
            schema_version=APPLICATION_ADAPTER_VERSION, application_id=APPLICATION_ID,
            # Vue Router owns its history entry's current/back/position fields. The
            # generic SPA goto cannot manufacture that state, so that mode stays off. The
            # application's page module opens a page through the router itself
            # (`openPage`); a page it does not open is loaded as a document.
            display_name="芋道管理系统", origin=self.origin, navigation_mode="document",
            page_write_guard=page_write_guard,
            page_controller_module=self.origin + (NATIVE_PAGE_CONTROLLER_PATH if native_tabs else PAGE_CONTROLLER_PATH),
            catalog_sha256=self.business_tools.catalog_sha256)

    def health(self) -> dict:
        try:
            user = (self.client.get("system/auth/get-permission-info") or {}).get("user") or {}
        except Exception as exc:  # the health check reports, it never raises
            return {"passed": False, "error": type(exc).__name__}
        return {"passed": bool(user.get("id")), "actor": str(user.get("id") or "")}


__all__ = ["APPLICATION_ID", "PAGE_CONTROLLER_PATH", "SESSION_HANDOFF_KEYS", "YudaoApplication",
           "YudaoIdentityPort"]
