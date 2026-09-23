"""Entry observation, planning, control installation, and resource closure."""

from .grounding import PlanState
from ..modeling.egress import ProviderEgressError
from ..planning.contract import (ContractPlanner, PlanningFailed,
                                 _segment_from_template, expand_terms)
from ..planning.retrieval import EvidenceBundle, SourceRetriever
from ..schemas import ControlPlan, OperationContract, PageObservation


class SessionLifecycle:
    """Pre-loop lifecycle behavior for a server-owned agent session."""

    def observe_entry(self) -> PageObservation:
        """First look at the running application, before anything is planned."""
        return self.adopt_entry(self.browser.observe())

    def adopt_entry(self, obs: PageObservation) -> PageObservation:
        """Accept an already-fresh entry observation and derive what it tells the planner.

        Planning needs one runtime fact that no descriptor can supply: which source-contract
        scopes this deployment actually exposes. The Adapter derives them from entry-page
        navigation, so the core does not interpret an application's URL grammar.

        A product turn already holds a fresh observation from the runtime handshake and must
        not spend a second browser round trip to get one; assigning `entry_observation`
        directly, as it used to, skipped this derivation and left `served_webapps` empty --
        which silently made every source affordance candidate empty in the product path while
        the same code produced candidates in a session composed directly.
        """
        self.entry_observation = obs
        self._remember_active_filters(obs)
        self.served_webapps = set(
            self.application_routes.discover_source_scopes(obs)
            if self.application_routes is not None else ())
        self.ledger.append("entry_observed", url=obs.url, epoch=obs.epoch,
                           served_webapps=sorted(self.served_webapps))
        return obs

    def plan(self) -> OperationContract:
        """Produce the contract for this arm. The arms differ here and nowhere else."""
        try:
            return self._plan()
        except (PlanningFailed, ProviderEgressError) as e:
            self.planning_error = str(e)[:800]
            self.ledger.append("planning_failed", detail=self.planning_error)
            return self.contract

    def _plan(self) -> OperationContract:
        source_contract = self.source_contract
        if self.oracle_contract is not None:
            self.contract = self.oracle_contract
            self.contract_source = "oracle"
        elif source_contract or self.plan_from_page:
            if source_contract:
                retriever = SourceRetriever(self.source_map)
                terms = expand_terms(self.chat, self.task.prompt, self.run_id,
                                     cache_dir=self.terms_cache)
                self.ledger.append("query_expanded", terms=list(terms))
                self.bundle = retriever.retrieve(self.task.task_ref, self.task.prompt,
                                                 webapps=self.served_webapps, extra_terms=terms)
            else:
                self.bundle = EvidenceBundle(task_ref=self.task.task_ref)
            # Statistics describe evidence volume, not content. Preserve the exact lines the
            # planner received because retrieval and model-expanded queries need not reproduce
            # the same slice later.
            self.ledger.append("evidence_retrieved", **self.bundle.stats,
                               evidence=list(self.bundle.lines))
            segment = (_segment_from_template(self.application_routes.contract_route_template)
                      if self.application_routes is not None else "")
            planner = ContractPlanner(self.chat, self.source_map if source_contract else None,
                                      segment=segment)
            # The planner receives the same public task, scope and current entry observation
            # as the acting agent. The test suite guards this complete input contract.
            self.contract, attempts = planner.plan(self.task.task_ref, self.task.public_text(),
                                                   self.bundle, self.run_id,
                                                   page=self.entry_observation)
            self.contract_source = "compiled" if source_contract else "page"
            self.ledger.append("contract_planned", attempts=attempts,
                               digest=self.contract.content_digest)
        else:
            self.contract_source = "none"
        # The whole contract, not just its digest. Reading a run afterwards means asking
        # "what was this agent told?", and a hash cannot answer that; reconstructing it is
        # impossible because the provider is not reproducible.
        self.ledger.append("contract_ready", source=self.contract_source,
                           digest=self.contract.content_digest,
                           citations=len(self.contract.cited_keys()),
                           unknowns=len(self.contract.unknowns),
                           contract=self.contract.to_json())
        self.plan_state = PlanState.of(
            self.contract, route_identity=self._contract_route_identity)
        return self.contract

    def install_control_plan(self, plan: ControlPlan) -> None:
        self._writes.begin_control()
        try:
            installed = self.control.install(plan)
            # Redacted on purpose: the ledger proves a plan existed and was stable, and reveals
            # nothing a reader could use to reconstruct the authorised write.
            self.ledger.append("control_plan_installed", plan=plan.redacted(),
                               tickets_installed=len(installed))
        except Exception:
            self._writes.end_control()
            raise

    def close(self) -> None:
        """Release external run-scoped resources when execution never reached ``run``."""
        self._writes.end_control()

    def _cancel_requested(self) -> bool:
        try:
            return bool(self.cancelled())
        except Exception as exc:
            self.ledger.append(
                "cancellation_check_failed", error_type=type(exc).__name__)
            return True
