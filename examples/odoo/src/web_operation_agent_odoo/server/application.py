"""Composition ports and top-level application adapter for Odoo 18."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.request import Request, urlopen

from web_operation_agent.business.mutation import MutationJournal
from web_operation_agent.business.entities import (DESCRIBE_ENTITY_TOOL,
                                                   DISCOVER_ENTITIES_TOOL,
                                                   QUERY_ENTITY_RECORDS_TOOL,
                                                   RESOLVE_RECORD_REFERENCE_TOOL)
from web_operation_agent.business.page_entries import FIND_PAGE_ENTRIES_TOOL
from web_operation_agent.business.goal_candidates import RESOLVE_GOAL_CANDIDATES_TOOL
from web_operation_agent.business.errors import BusinessToolError
from web_operation_agent.business.runtime import (BusinessSkill,
                                                  BusinessToolContractError,
                                                  BusinessToolRuntime,
                                                  ForbiddenOperation, ToolOperation,
                                                  business_text_key)
from web_operation_agent.composition.descriptor import (
    APPLICATION_ADAPTER_VERSION,
    ApplicationDescriptor,
)
from web_operation_agent.composition.ports import (
    ApplicationPorts,
    DeclaredDataClassifier,
    MutationReconciliation,
    ResultPostconditionVerifier,
)
from web_operation_agent.composition.sources import SourceCapability
from web_operation_agent.planning.repository_source import (CombinedSourceLookup,
                                                            RepositorySourceLookup)
from web_operation_agent.planning.source_facts import CompiledSourceLookup
from web_operation_agent.planning.source_history import GitSourceHistory
from web_operation_agent.state.identity import (IdentityAssertion, PrincipalContext,
                                                PrincipalError, bind_matching_identity)

from ..route import ODOO_ROUTE_PORT, ODOO_SOURCE_SCOPE
from ..sourcemap import OdooCompiler, module_of, odoo_source_tree
from .executor import OdooBusinessToolExecutor
from .descriptors import OdooRuntimeDescriptorReader
from .identity import OdooApiKeyStore, OdooPageSessionIdentityResolver
from .generic_forms import OdooFormPort
from ..click_policy import OdooReviewedClickPolicy, load_review
from .specs import (ANSWER_LABELS_ZH, APPLICATION_ID, ARCHIVE_CUSTOMER_TOOL,
                    COLLECTIONS, CREATE_CUSTOMER_TOOL, MUTATION_SPECS, MUTATION_TOOLS,
                    PREPARE_CUSTOMER_EDIT_TOOL, PREPARE_CUSTOMER_FORM_TOOL,
                    _CUSTOMERS, _MAX_RESPONSE_BYTES, _READ_SKILLS, _WRITE_SKILLS,
                    _packaged_skill, _validated_origin)

#: Served by the woa_runtime addon (1.5 and later): while the Agent acts on the page, every
#: request of the person's session runs in a read-only transaction and a write is refused.
PAGE_WRITE_GUARD = "/woa/page-guard"

#: What each business tool does, in the core's entity-and-operation vocabulary. The server
#: records it on every result, and a deployment's forbidden effect classes are checked
#: against it when the catalog is built.
TOOL_OPERATIONS = {
    "query_sale_orders": ("sales_order", "read"),
    "query_quotations": ("sales_order", "read"),
    "query_customers": ("customer", "read"),
    "query_customer_invoices": ("customer_invoice", "read"),
    "query_products": ("product", "read"),
    "discover_entities": ("runtime_record", "read"),
    "describe_entity": ("runtime_record", "read"),
    "query_entity_records": ("runtime_record", "read"),
    "resolve_record_reference": ("runtime_record", "read"),
    "find_page_entries": ("runtime_page", "read"),
    "resolve_goal_candidates": ("runtime_record", "read"),
    "prepare_customer_form": ("customer", "read"),
    "prepare_customer_edit_form": ("customer", "read"),
    "create_customer": ("customer", "create"),
    "archive_customer": ("customer", "archive"),
    "confirm_quotation": ("sales_order", "state_change"),
    "invoice_sale_order": ("customer_invoice", "issue_document"),
}

#: Effect classes this adapter refuses to install at all. Deletion is not reversible and
#: this application never needs it; archiving is the supported way to retire a record.
FORBIDDEN_OPERATIONS = (
    ForbiddenOperation(operation="delete",
                       reason="Odoo records are archived, never deleted, by this assistant"),
)


class _OdooIdentityPort:
    required = True

    def __init__(self, executor: OdooBusinessToolExecutor,
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
            raise PrincipalError("Odoo requires a verified application principal")
        principal.require_live(application_id=application_id)


class _OdooMutationReconciler:
    """What the application can still say about a write whose outcome was lost.

    Never a verdict. Every one of these returns `unknown` with `requires_human`: a matching
    record is not proof that *this* proposal created it, and a missing one cannot rule out a
    write still in flight. What it does supply is the evidence an operator needs to decide,
    gathered under the same delegated actor, over a scan that says whether it finished.
    """

    #: Each write and the delegated read scope its evidence needs. This must be the same
    #: catalog that selected its dispatch/readback implementation.
    _SCOPES = {spec.name: spec.reconciliation_scope for spec in MUTATION_SPECS}

    def __init__(self, executor: OdooBusinessToolExecutor, enabled: bool):
        self._executor = executor
        self.supported_tools = frozenset(self._SCOPES) if enabled else frozenset()

    def reconcile(self, name, business_key, *, evidence, principal):
        if name not in self.supported_tools or principal is None:
            raise ValueError("reconciliation requires an enabled tool and principal")
        principal.require_live(application_id=APPLICATION_ID)
        needed = self._SCOPES[name]
        if needed not in principal.scopes:
            raise PrincipalError(f"reconciliation requires {needed} scope")
        delegated = self._executor.resolve_delegated_identity()
        if any(getattr(principal, field) != getattr(delegated, field)
               for field in ("tenant_id", "workspace_id", "actor_id")):
            raise PrincipalError("reconciliation delegated identity changed")
        if name == CREATE_CUSTOMER_TOOL:
            reason = self._customer_evidence(business_key)
        elif name == ARCHIVE_CUSTOMER_TOOL:
            reason = self._customer_archive_evidence(business_key)
        else:
            reason = self._order_evidence(name, business_key)
        return MutationReconciliation(
            outcome="unknown", requires_human=True,
            reason=reason + " Establish the original writer's final outcome before "
                            "resolving.")

    def _customer_evidence(self, business_key) -> str:
        args = self._executor._normalize_customer(business_key)
        rows, total, scan = self._executor._scan(_CUSTOMERS)
        matches = [row for row in rows
                   if business_text_key(self._executor._label(row, "name"))
                   == business_text_key(args["name"])
                   and self._executor._label(row, "email").casefold() == args["email"]]
        return (f"Current matching records: {len(matches)}; complete scan: "
                f"{scan['complete'] and len(rows) == total}.")

    def _customer_archive_evidence(self, business_key) -> str:
        record_id = self._executor._customer_record_id(business_key)
        rows = self._executor.call(
            "res.partner", "read", [[record_id]],
            {"fields": ["name", "active"], "context": {"active_test": False}})
        if not isinstance(rows, list):
            raise BusinessToolContractError("Odoo returned an invalid reconciliation read")
        if len(rows) != 1 or not isinstance(rows[0], dict):
            return f"Customer record {record_id} is not currently readable."
        return (f"Customer record {record_id} ({self._executor._label(rows[0], 'name')!r}) "
                f"currently has active={rows[0].get('active')!r}.")

    def _order_evidence(self, name: str, business_key) -> str:
        """For both order writes the evidence is the order's own current state.

        A confirm is visible as `state`, an invoice as the order's `invoice_ids`: both are
        facts the application owns, read now, rather than anything this process remembers.
        """
        reference = self._executor._reference(name, business_key)
        rows = self._executor.call(
            "sale.order", "search_read", [[["name", "=", reference]]],
            {"fields": ["name", "state", "invoice_status", "invoice_ids"], "limit": 5})
        if not isinstance(rows, list):
            raise BusinessToolContractError("Odoo returned an invalid reconciliation read")
        if len(rows) != 1:
            return (f"{len(rows)} sales orders currently carry the reference {reference}.")
        row = rows[0]
        invoices = row.get("invoice_ids")
        return (f"Sales order {reference} is currently in state "
                f"{self._executor._label(row, 'state')!r} with invoice status "
                f"{self._executor._label(row, 'invoice_status')!r} and "
                f"{len(invoices) if isinstance(invoices, list) else 0} linked invoices.")


class OdooApplicationAdapter:
    """Application-owned facts and tools for the Odoo 18 reference integration."""

    def __init__(self, *, base_url: str, api_key_path: str | Path,
                 allow_mutations: bool = False,
                 mutation_journal: MutationJournal | None = None,
                 page_controller_module: str | None = None,
                 timeout_s: float = 20,
                 public_origin: str | None = None,
                 allow_private_http: bool = False,
                 extra_skills: tuple[BusinessSkill, ...] = ()):
        if not isinstance(allow_mutations, bool):
            raise ValueError("allow_mutations must be a boolean")
        if allow_mutations and mutation_journal is None:
            raise ValueError("allow_mutations=True requires an explicit mutation journal")
        self.origin = _validated_origin(public_origin or base_url)
        self.rpc_origin = _validated_origin(
            base_url, allow_private_http=allow_private_http)
        self.page_controller_module = (
            page_controller_module or self.origin
            + "/woa_runtime/static/src/page-controller-with-state.js")
        executor = OdooBusinessToolExecutor(
            base_url=self.rpc_origin, keys=OdooApiKeyStore(api_key_path),
            timeout_s=timeout_s, allow_mutations=allow_mutations,
            allow_private_http=allow_private_http)
        # Discovery metadata is actor-scoped and inert: it grants neither a business read
        # nor a page/write action. Phase 8 consumers must still pass through the ordinary
        # typed evidence and mutation boundaries before this can widen a turn.
        self.runtime_descriptors = OdooRuntimeDescriptorReader(executor)
        # Read-only Skill artifacts supplied by the host -- an evaluation's hand-written
        # procedure, say. They reuse the packaged typed tools and add no capability.
        if (not isinstance(extra_skills, tuple)
                or any(not isinstance(skill, BusinessSkill) or not skill.read_only
                       for skill in extra_skills)):
            raise TypeError("extra_skills must be read-only Skill artifacts")
        skills = [_packaged_skill(name) for name in _READ_SKILLS] + list(extra_skills)
        withheld: list[BusinessSkill] = []
        if allow_mutations:
            skills += [_packaged_skill(name) for name in _WRITE_SKILLS]
        else:
            # Installed here, not granted to this actor. The catalog this session can run
            # omits them; a refusal that did not know about them would tell the person the
            # deployment cannot do what it does every day (P7.5 CR-37).
            withheld = [_packaged_skill(name) for name in _WRITE_SKILLS]
        required_scopes = {"query_sale_orders": "sale_orders:read",
                           "query_customers": "customers:read",
                           "query_products": "products:read",
                           "query_quotations": "sale_orders:read",
                           "query_customer_invoices": "invoices:read"}
        required_scopes.update({
            DISCOVER_ENTITIES_TOOL: "generic_records:read",
            DESCRIBE_ENTITY_TOOL: "generic_records:read",
            QUERY_ENTITY_RECORDS_TOOL: "generic_records:read",
            RESOLVE_RECORD_REFERENCE_TOOL: "generic_records:read",
            FIND_PAGE_ENTRIES_TOOL: "generic_records:read",
            RESOLVE_GOAL_CANDIDATES_TOOL: "generic_records:read",
        })
        required_scopes[PREPARE_CUSTOMER_FORM_TOOL] = "customers:read"
        if allow_mutations:
            # It stages a change to an existing customer, so it needs the same scope a
            # write does, even though the application is what commits it.
            required_scopes[PREPARE_CUSTOMER_EDIT_TOOL] = "customers:write"
            required_scopes.update({spec.name: spec.write_scope for spec in MUTATION_SPECS})
        declarations = [
            (collection.tool,
             (collection.data_class, "business.identifier", "application.route"))
            for collection in COLLECTIONS]
        declarations.append((
            PREPARE_CUSTOMER_FORM_TOOL,
            ("business.customer", "application.route", "business.form_draft")))
        declarations.extend((
            (DISCOVER_ENTITIES_TOOL, ("application.metadata",)),
            (DESCRIBE_ENTITY_TOOL, ("application.metadata",)),
            (QUERY_ENTITY_RECORDS_TOOL, ("business.generic_record", "business.identifier")),
            (RESOLVE_RECORD_REFERENCE_TOOL,
             ("business.generic_record", "business.identifier", "application.route")),
            (FIND_PAGE_ENTRIES_TOOL, ("application.metadata", "application.route")),
            (RESOLVE_GOAL_CANDIDATES_TOOL,
             ("application.metadata", "application.route", "business.identifier")),
        ))
        if allow_mutations:
            declarations.append((
                PREPARE_CUSTOMER_EDIT_TOOL,
                ("business.customer", "application.route", "business.form_draft")))
            declarations += [(spec.name, spec.data_types) for spec in MUTATION_SPECS]
        data = DeclaredDataClassifier(tuple(sorted(declarations)))
        # Every write must be able to say the application confirmed it, under the same user.
        # A mutation without this pair in its result is refused by the core before it can be
        # reported as done.
        postconditions = ResultPostconditionVerifier(
            tuple((tool, ("content.readback_verified", "audit.readback_verified"))
                  for tool in MUTATION_TOOLS)
            if allow_mutations else ())
        routes = ODOO_ROUTE_PORT
        self.business_tools = BusinessToolRuntime(
            tuple(skills), executor, application_id=APPLICATION_ID,
            required_scopes=required_scopes, mutation_journal=mutation_journal,
            data_classifier=data, postcondition_verifier=postconditions,
            route_validator=routes,
            route_skill_hints=((r"^/odoo/customers(?:/[^/?#]+)?$",
                                "odoo.sales.customers-query"),),
            withheld_skills=tuple(withheld),
            operations={name: ToolOperation(entity, operation)
                        for name, (entity, operation) in TOOL_OPERATIONS.items()
                        if name in {schema["function"]["name"]
                                    for schema in executor.tool_schemas()}},
            forbidden_operations=FORBIDDEN_OPERATIONS,
            answer_labels={"zh": ANSWER_LABELS_ZH},
            numeric_goal_binding=True)
        self._executor = executor
        self._click_policy: OdooReviewedClickPolicy | None = None
        # The form port recognizes bounded create pages. It declares the draft's
        # persistence semantics; save-on-leave edits require approval before input.
        # Metadata alone cannot certify arbitrary custom onchange side effects.
        forms = OdooFormPort(executor)
        self.ports = ApplicationPorts(
            identity=_OdooIdentityPort(
                executor,
                frozenset(required_scopes.values()) | forms.required_scopes),
            routes=routes,
            postconditions=postconditions,
            reconciliation=_OdooMutationReconciler(executor, allow_mutations),
            data=data,
            forms=forms,
        )
        self.descriptor = ApplicationDescriptor(
            schema_version=APPLICATION_ADAPTER_VERSION,
            application_id=APPLICATION_ID,
            display_name="Odoo 18",
            origin=self.origin,
            navigation_mode="spa",
            page_controller_module=self.page_controller_module,
            catalog_sha256=self.business_tools.catalog_sha256,
            # woa_runtime 1.5: page actions run with this session read-only.
            page_write_guard=PAGE_WRITE_GUARD,
        )

    def source_capability(self, source_root: str | Path | None = None, *,
                          release: str = "",
                          attestation_id: str = "",
                          deployed_commit: str = "") -> SourceCapability:
        """Compile this deployment's Odoo source once and return what it enables.

        This is the adapter handing the composition a decision, not a deployment picking
        four flags by hand. With no source root -- the ordinary case, since source retrieval
        is optional -- the answer is "nothing", and the session runs on runtime facts alone.
        """
        if source_root is None:
            return SourceCapability.none()
        if not release:
            raise ValueError("an Odoo source root must be bound to an exact release")
        # Reject a missing extraction before making a live metadata call.  This keeps a
        # local deployment configuration error distinct from an unavailable application.
        if not odoo_source_tree(source_root, release=release).available():
            raise ValueError("the Odoo source root does not exist")
        manifest = self.runtime_descriptors.source_manifest()
        release_binding = "sha256:" + hashlib.sha256(json.dumps(
            {"build_release": release, "module_digest": manifest.module_digest},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        tree = odoo_source_tree(source_root, release=release_binding)
        installed_modules = frozenset(item.name for item in manifest.modules)
        ownership_files = frozenset({"CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS"})

        def deployed_path(path: str) -> bool:
            return module_of(path) in installed_modules or path in ownership_files

        source_map = OdooCompiler(
            tree, attestation_id or f"odoo:{release_binding}",
            modules=installed_modules).compile()
        repository_lookup = RepositorySourceLookup(tree, path_filter=deployed_path)
        history, history_reason = GitSourceHistory.optional(
            tree, deployed_commit, path_filter=deployed_path)
        return SourceCapability(
            source_map=source_map,
            # Odoo's writes are button methods with no URL of their own, so the legacy
            # route-shaped operation contract has nothing to plan over here. What the map
            # does support is view-only candidates and cited runtime lookup.
            source_contract=False,
            runtime_source=True,
            source_affordance_mode="auto",
            lookup=CombinedSourceLookup(
                CompiledSourceLookup(source_map), repository_lookup),
            history=history,
            history_unavailable_reason=history_reason,
        )

    def click_policy(self) -> "OdooReviewedClickPolicy":
        """The reviewed read-only click policy, resolved against this database once.

        Reviewed actions are named by XML ID; their numbers are this deployment's. An XML ID
        that does not resolve -- a module not installed, an unreachable API -- is left out,
        so the policy only ever narrows to what could be confirmed here.
        """
        if self._click_policy is None:
            review = load_review()
            names = {item["xmlid"] for item in review["actions"]}
            resolved: dict[str, int] = {}
            paths: dict[str, str] = {}
            try:
                # The runtime descriptor first: reading `ir.model.data` or the action table
                # directly needs Settings rights, so an ordinary salesperson resolved
                # nothing and lost every reviewed page. The addon reports the actions this
                # actor's own menus reach, with their XML IDs and URL paths.
                for action in self.runtime_descriptors.catalog().actions:
                    if action.xml_id in names:
                        resolved[action.xml_id] = action.action_id
                        if not action.route.startswith("action-"):
                            paths[action.xml_id] = action.route
            except (BusinessToolError, BusinessToolContractError):
                resolved, paths = {}, {}
            missing = names - set(resolved)
            if missing:
                # A reviewed page with no menu entry -- the reason a person asks the
                # assistant for it at all -- is not in any actor's menu tree. Ask the
                # registry directly, which an administrator may read and an ordinary user
                # may not: what stays unresolved is simply not reviewed for that actor.
                resolved.update(self._actions_by_registry(missing, paths))
            policy = OdooReviewedClickPolicy.from_review(review, resolved, paths)
            # A Skill route carries a typed result's route authority, and Odoo's route
            # contract accepts any numeric action -- including a server action, which runs
            # code when opened. So on Odoo a Skill may declare only pages reviewed as
            # read-only; anything else refuses the composition rather than the click.
            for skill in self.business_tools.skills:
                unreviewed = [route for route in skill.routes
                              if not policy.destination_allowed(route)]
                if unreviewed:
                    raise ValueError(
                        f"Skill {skill.skill_id} declares routes that are not reviewed "
                        f"read-only pages on this deployment: {unreviewed}")
            self._click_policy = policy
        return self._click_policy

    def _actions_by_registry(self, names: set[str], paths: dict[str, str]) -> dict[str, int]:
        """Resolve reviewed XML IDs through `ir.model.data`; empty when this actor cannot.

        `complete_name` is not stored, so Odoo cannot search it: ask by module and name and
        keep only the exact pairs the review names.
        """
        resolved: dict[str, int] = {}
        try:
            rows = self._executor.call(
                "ir.model.data", "search_read",
                [[["model", "=", "ir.actions.act_window"],
                  ["module", "in", sorted({name.split(".", 1)[0] for name in names})],
                  ["name", "in", sorted({name.split(".", 1)[1] for name in names})]]],
                {"fields": ["module", "name", "res_id"]})
            for row in rows if isinstance(rows, list) else ():
                if not isinstance(row, dict) or not isinstance(row.get("res_id"), int):
                    continue
                xmlid = f"{row.get('module')}.{row.get('name')}"
                if xmlid in names:
                    resolved[xmlid] = row["res_id"]
            if resolved:
                by_id = {action_id: xmlid for xmlid, action_id in resolved.items()}
                actions = self._executor.call(
                    "ir.actions.act_window", "read", [sorted(by_id)], {"fields": ["path"]})
                for action in actions if isinstance(actions, list) else ():
                    if (isinstance(action, dict) and action.get("id") in by_id
                            and isinstance(action.get("path"), str) and action["path"]):
                        paths[by_id[action["id"]]] = action["path"]
        except BusinessToolError:
            return {}
        return resolved

    @property
    def source_scopes(self) -> frozenset[str]:
        """Odoo serves one web client, so every route lives in one scope."""
        return frozenset({ODOO_SOURCE_SCOPE})

    def health(self) -> dict:
        request = Request(self.rpc_origin + "/web/webclient/version_info",
                          data=json.dumps({"jsonrpc": "2.0", "method": "call",
                                           "params": {}}).encode("utf-8"), method="POST")
        request.add_header("Content-Type", "application/json")
        try:
            with urlopen(request, timeout=5) as response:
                payload = json.loads(response.read(_MAX_RESPONSE_BYTES))
                info = payload.get("result") if isinstance(payload, dict) else None
                version = info.get("server_version") if isinstance(info, dict) else None
                return {"passed": response.status == 200 and isinstance(version, str),
                        "http_status": response.status,
                        "server_version": version if isinstance(version, str) else ""}
        except Exception:  # noqa: BLE001 — health is a boolean, never a leak
            return {"passed": False, "http_status": 0, "server_version": ""}

    def bind_principal(self, page_identity: IdentityAssertion, *, delegation_id: str,
                       lifetime_s: int = 300,
                       now_ms: int | None = None,
                       scopes: tuple[str, ...] | None = None) -> PrincipalContext:
        """Match a trusted Odoo page-session assertion to the delegated API-key actor.

        The host must obtain ``page_identity`` from Odoo's own authenticated session. DOM
        text or a browser-supplied user id is not trusted evidence.
        """
        if scopes is None:
            return self.ports.identity.bind(
                page_identity, delegation_id=delegation_id, lifetime_s=lifetime_s,
                now_ms=now_ms)
        selected = frozenset(scopes)
        if (not isinstance(scopes, tuple) or not scopes or len(selected) != len(scopes)
                or not selected <= self.ports.identity.required_scopes):
            raise PrincipalError("deployment scopes must be a non-empty unique subset")
        delegated = self._executor.resolve_delegated_identity(
            lifetime_s=lifetime_s, now_ms=now_ms)
        return bind_matching_identity(
            page_identity, delegated, delegation_id=delegation_id,
            scopes=tuple(sorted(selected)), now_ms=now_ms)

    def bind_browser_session(self, storage_state_path: str | Path, *, delegation_id: str,
                             lifetime_s: int = 300,
                             now_ms: int | None = None,
                             scopes: tuple[str, ...] | None = None) -> PrincipalContext:
        """Bind the exact browser storage state to the delegated API-key identity."""
        page = OdooPageSessionIdentityResolver(
            base_url=self.origin, storage_state_path=storage_state_path,
            timeout_s=self._executor.timeout_s).resolve(
                lifetime_s=lifetime_s, now_ms=now_ms)
        return self.bind_principal(page, delegation_id=delegation_id,
                                   lifetime_s=lifetime_s, now_ms=now_ms,
                                   scopes=scopes)

    def page_session_binding(self, storage_state: str | Path | dict) -> str:
        """Hash the active Odoo session cookie without exposing it to the Agent."""
        source = ({"storage_state": storage_state} if isinstance(storage_state, dict)
                  else {"storage_state_path": storage_state})
        return OdooPageSessionIdentityResolver(
            base_url=self.origin, timeout_s=self._executor.timeout_s,
            **source).binding_digest()
