"""ServerAgentSession: the one logical agent.

It holds the source evidence, the contract, the model loop,
the runtime grounding that overrides the contract when the page disagrees, the hidden
control plan and the append-only ledger. The browser holds a page.

Two execution invariants govern the session.

*Runtime facts win.* A contract is a prior. After every action the observed page is
compared with what the contract predicted, and a mismatch is written into the model's
context as a correction, not swallowed. The source says what is possible; the deployment
says what is true (architecture contract #4).

*Uncertainty is not retried.* If a mutation may or may not have landed -- the control plane
consumed a permit but the browser lost the page, say -- the run ends as
`indeterminate_write`. It is not scored and it is not attempted again.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import time
import uuid
from typing import Callable
from zoneinfo import ZoneInfo

from ..application import ApplicationRoutePort, SourceCapability
from ..answer import answer_protocol
from ..business.approvals import ApprovalWatch, NavigationPlan
from ..answer.answer_view import (capability_gap_answer, render_payload, unavailable_answer,
                          withdrawn_answer)
from ..affordance import SourceAffordance, resolve_source_affordances
from ..transport.browser import BrowserExecutor
from ..business.runtime import (BUSINESS_SKILL_MATCHER_VERSION, BusinessSkillSelection,
                               FormDraft, BusinessToolRuntime)
from ..business.errors import BusinessToolPermissionError
from ..business.text import trigger_requests_action
from ..planning.contract import (ContractPlanner, PlanningFailed, _segment_from_template,
                                expand_terms)
from ..planning.retrieval import EvidenceBundle, SourceRetriever
from ..controlplane import ControlPlaneClient, ControlPlaneError
from ..modeling.egress import AuditedChat, ProviderEgressError
from ..grounding import (NOT_APPLICABLE, REFUTED, WRITE_PERMITTED, WRITE_REFUSED,
                        PlanState, opaque_route_identity)
from ..state.identity import PrincipalContext
from ..planning.knowledge import ApplicationKnowledgePort
from ..state.ledger import SessionLedger
from ..privacy import SessionHandoffPolicy, safe_tool_arguments as _safe
from .routes import Router, candidate_url
from .turnwatch import ProgressFeed, RepeatGuard
from .turn_answer import AnswerState, TurnAnswer
from .tools import ToolSurface
from .business_tools import render_business_skills
from . import knowledge_tools
from .knowledge_tools import active_capability_digest
from .loop import run_loop
from .runstate import (CAPABILITY_GAP_TERMINAL, LabApparatus, NO_LAB, RunResult,
                       TaskSpec, _Next, _Turn)
from ..business.writes import PRE_DISPATCH_ERRORS, WRITE_CAPABLE, WatchState, WriteWatch
from .prompts import (AGENT_SYSTEM, SCOPE_FIRST_SYSTEM, TOOL_SCHEMAS, _CAPABILITY_QUESTION,
                      _NAVIGATION_EXTRA_GOAL, _NAVIGATION_ONLY, _PLAN_PREAMBLE,
                      _REFERENCE_FOLLOWUP, _assistant_call, _tool_result, _trim)
from ..schemas import (BROWSER_ACTIONS, ActionReceipt, ControlPlan, OperationContract,
                      PageObservation, digest, empty_contract)
from ..answer.evidence import EvidenceUnknown

#: Describe a plan by its actual origin. A page-only plan must never be presented to the model
#: as source-derived, because that would change the information available to that mode.


def _validated_time_zone(value: object) -> str:
    """An IANA zone this machine can resolve, or nothing.

    A zone that does not resolve is refused rather than silently ignored: a turn that thinks
    it is answering in the person's day and is not is the defect, not the absence of a zone.
    """
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("time_zone must be an IANA zone name")
    try:
        ZoneInfo(value)
    except Exception as exc:  # ZoneInfoNotFoundError, and whatever a bad key raises
        raise ValueError(f"time_zone {value!r} is not a zone this machine knows") from exc
    return value


def _is_navigation_only(prompt: str) -> bool:
    return bool(_NAVIGATION_ONLY.fullmatch(prompt) and not _NAVIGATION_EXTRA_GOAL.search(prompt))


def _is_capability_gap(verified: dict | None) -> bool:
    """A verified answer that is exactly one capability gap and nothing else."""
    if not verified:
        return False
    conclusions = verified.get("conclusions") or ()
    return (len(conclusions) == 1
            and isinstance(conclusions[0], dict)
            and conclusions[0].get("kind") == "capability_gap")

#: The model decides whether the user asked for a change; the server decides whether this
#: session can make it. A search run for every turn would find no write capability behind
#: every read-only question -- a read matches no write Skill -- so the decline would be offered
#: to every hard question, where it does not belong. Called on the model's judgement, it
#: costs one call on the turns that need it and nothing on the rest.

#: Offered only on the turn that answers a question this session asked. The model recognises
#: that the reply drops the request; the server says so, and says it only while it is true.


#: One question offers at most this many choices. The tool schema, the check that reads a
#: model's ask_user, and the server's own shortcut all read the limit here: they disagreed,
#: and a complete read of nine records ended the turn with an error instead of a question.


# This optional policy is free of application names, routes, forms and identifiers. The
# `scope_first_policy` flag selects it without changing the default agent prompt.


class ServerAgentSession:
    """One task, one browser, one model, one audit ledger.

    The constructor takes three kinds of thing, and the signature says which is which --
    confusing them is how an experiment leaks into a product:

    * **`source`** -- a `SourceCapability`: what this deployment's compiled source lets a
      session do. A deployment does not pick its four settings by hand; they follow from
      whether the application distribution ships a `SourceCompiler` and whether that
      compiler ran for this release, which is why an adapter returns them as one value
      (`OdooApplicationAdapter.source_capability`). Omitting it means no compiled source.
    * **`lab`** -- a `LabApparatus`: settings that exist only so the comparison harness can
      define its arms, `oracle_contract` above all. `product.py` passes no `lab=`, and
      `test_product.LabApparatusNeverReachesTheProduct` keeps it that way by inspecting the
      keywords a product turn actually uses.
    * **everything else** -- product policy: budgets, disabled actions, approval, identity.

    These were eight loose keywords until the behaviour gold standard existed to prove the
    grouping changed nothing (`tests/test_session_golden.py`).
    """

    def __init__(self, *, task: TaskSpec, chat, browser: BrowserExecutor,
                 control: ControlPlaneClient, ledger: SessionLedger,
                 source: SourceCapability | None = None,
                 lab: LabApparatus = NO_LAB,
                 step_budget: int = 20, wall_budget_s: int = 300, run_id: str | None = None,
                 keep_observations: int = 3,
                 disabled_actions: frozenset[str] | set[str] | None = None,
                 scope_first_policy: bool = False,
                 page_answer_evidence: bool = False,
                 policy_block_limit: int | None = None,
                 no_progress_repeat_limit: int | None = None,
                 knowledge_router: ApplicationKnowledgePort | None = None,
                 allow_application_entry: bool = False,
                 application_routes: ApplicationRoutePort | None = None,
                 business_tools: BusinessToolRuntime | None = None,
                 business_mutation_approval=None,
                 navigation_confirmation=None,
                 allow_clarification: bool = False,
                 allow_form_prefill: bool = False,
                 deterministic_read_navigation: bool = False,
                 plain_business_answer_min_page_actions: int | None = None,
                 max_agent_said_chars: int = 500,
                 cancelled: Callable[[], bool] | None = None,
                 principal: PrincipalContext | None = None,
                 session_id: str = "", turn_id: str = "",
                 time_zone: str = "",
                 conversation_context: dict | None = None,
                 progress: Callable[[dict], None] | None = None):
        if source is not None and not isinstance(source, SourceCapability):
            raise TypeError("source must be a SourceCapability or None")
        if not isinstance(lab, LabApparatus):
            raise TypeError("lab must be a LabApparatus")
        # `SourceCapability.__post_init__` already refuses every incoherent combination --
        # a runtime lookup, an auto affordance mode or a source contract without a map --
        # so there is exactly one place those rules live.
        source = source if source is not None else SourceCapability.none()
        source_map = source.source_map
        source_contract = source.source_contract
        runtime_source = source.runtime_source
        source_affordance_mode = source.source_affordance_mode
        arm = lab.arm
        plan_from_page = lab.plan_from_page
        oracle_contract = lab.oracle_contract
        terms_cache = lab.terms_cache
        disabled = frozenset(disabled_actions or ())
        unknown_disabled = disabled - BROWSER_ACTIONS
        if unknown_disabled:
            raise ValueError(f"disabled actions are not browser tools: {sorted(unknown_disabled)}")
        if "observe" in disabled:
            raise ValueError("observe cannot be disabled: policy refusals need a fresh page")
        if (policy_block_limit is not None
                and (isinstance(policy_block_limit, bool)
                     or not isinstance(policy_block_limit, int)
                     or policy_block_limit < 1)):
            raise ValueError("policy block limit must be a positive integer or None")
        if (no_progress_repeat_limit is not None
                and (isinstance(no_progress_repeat_limit, bool)
                     or not isinstance(no_progress_repeat_limit, int)
                     or no_progress_repeat_limit < 2)):
            raise ValueError(
                "no progress repeat limit must be at least two or None")
        if (plain_business_answer_min_page_actions is not None
                and (isinstance(plain_business_answer_min_page_actions, bool)
                     or not isinstance(plain_business_answer_min_page_actions, int)
                     or plain_business_answer_min_page_actions < 0)):
            raise ValueError(
                "plain business answer minimum page actions must be nonnegative or None")
        if (isinstance(max_agent_said_chars, bool)
                or not isinstance(max_agent_said_chars, int)
                or not 1 <= max_agent_said_chars <= 20_000):
            raise ValueError("max_agent_said_chars must be between 1 and 20000")
        if progress is not None and not callable(progress):
            raise ValueError("progress must be callable or None")
        if cancelled is not None and not callable(cancelled):
            raise ValueError("cancelled must be callable or None")
        if not isinstance(allow_form_prefill, bool):
            raise ValueError("allow_form_prefill must be a boolean")
        if principal is not None and not isinstance(principal, PrincipalContext):
            raise ValueError("principal must be a PrincipalContext or None")
        if principal is not None and (not session_id or not turn_id):
            raise ValueError("principal-bound sessions require session_id and turn_id")
        if principal is None and (session_id or turn_id):
            raise ValueError("session_id and turn_id require a principal")
        if not isinstance(browser, BrowserExecutor):
            raise TypeError("browser must implement the BrowserExecutor contract")
        if conversation_context is not None:
            if not isinstance(conversation_context, dict):
                raise ValueError("conversation_context must be a JSON object or None")
            try:
                encoded_conversation = json.dumps(
                    conversation_context, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), allow_nan=False).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise ValueError("conversation_context must be finite JSON") from exc
            if len(encoded_conversation) > 32 * 1024:
                raise ValueError("conversation_context exceeds 32 KiB")
            self.conversation_context = json.loads(encoded_conversation)
        else:
            self.conversation_context = None
        if application_routes is not None and not isinstance(
                application_routes, ApplicationRoutePort):
            raise TypeError("application_routes must implement ApplicationRoutePort or be None")
        if knowledge_router is not None and not isinstance(
                knowledge_router, ApplicationKnowledgePort):
            raise TypeError("knowledge_router must implement ApplicationKnowledgePort or be None")
        source_routes_enabled = bool(
            runtime_source or source_affordance_mode == "auto" or source_contract)
        if application_routes is None and (
                source_routes_enabled or knowledge_router is not None or allow_application_entry):
            raise ValueError(
                "source navigation and application entry require an application route port")
        self.application_routes = application_routes
        # The static baseline in `privacy.py` knows no application's session parameters.
        # Widening it here is what keeps an adapter's own secret out of the audit ledger.
        self.session_handoffs = SessionHandoffPolicy.extending(
            application_routes.session_handoff_query_keys
            if application_routes is not None else ())
        self._contract_route_identity = (
            application_routes.contract_route_identity
            if application_routes is not None else opaque_route_identity)
        self.task = task
        self.arm = arm
        self.chat = AuditedChat(chat, ledger)
        self.browser = browser
        self.control = control
        self.ledger = ledger
        self.source = source
        self.source_map = source_map
        #: Whether this run compiles its contract from cited source. A capability may carry a
        #: map for runtime resolution only, so this is declared rather than inferred.
        self.source_contract = source_contract
        # Explicit opt-in for runtime source retrieval.
        self.runtime_source = runtime_source
        # ``auto`` and ``empty`` are an experimental pair with byte-identical tool schemas.
        # Empty exposes no SourceMap facts; it exists so a page-only comparison does not hand
        # the treatment arm an extra action vocabulary.
        self.source_affordance_mode = source_affordance_mode
        self.scope_first_policy = bool(scope_first_policy)
        self._runtime_retriever = SourceRetriever(source_map) if runtime_source else None
        self._source_view_routes: dict[tuple[str, str], set[str]] = {}
        self._source_affordances: dict[str, SourceAffordance] = {}
        self.knowledge_router = knowledge_router
        self._task_skills = (knowledge_router.relevant(task.public_text())
                             if knowledge_router is not None else ())
        self._task_skill_ids = {skill.skill_id for skill in self._task_skills}
        self.allow_application_entry = bool(allow_application_entry)
        #: Which exact page request a navigation tool may make. Everything it answers with
        #: comes from something installed for this session, never from what the model said.
        self._router = Router(
            ledger=ledger, application_routes=application_routes,
            knowledge_router=knowledge_router, source_map=source_map,
            session_handoffs=self.session_handoffs,
            route_identity=self._contract_route_identity,
            allow_application_entry=self.allow_application_entry,
            source_affordance_mode=source_affordance_mode)
        self.business_tools = business_tools
        #: Where the turn reports what it is doing, between page actions. Nobody may be
        #: listening -- every apparatus without a panel -- and the run does not depend on it.
        self._progress = progress
        self._feed = ProgressFeed(progress, on_failure=self.ledger.append)
        mutations_enabled = business_mutation_approval is not None
        if navigation_confirmation is not None and not callable(navigation_confirmation):
            raise TypeError("navigation_confirmation must be callable or None")
        self._capability_question = bool(_CAPABILITY_QUESTION.fullmatch(task.prompt))
        if not isinstance(allow_clarification, bool):
            raise ValueError("allow_clarification must be a boolean")
        if not isinstance(deterministic_read_navigation, bool):
            raise ValueError("deterministic_read_navigation must be a boolean")
        self.allow_clarification = allow_clarification
        self.allow_form_prefill = allow_form_prefill
        self.deterministic_read_navigation = deterministic_read_navigation
        current_navigation_only = _is_navigation_only(task.prompt)
        self._navigation_only = current_navigation_only
        # A clarification reply such as "Azure Interior" carries no domain verb of its
        # own. The unresolved turn is the server-owned proof that it is a continuation;
        # recover only that turn's user-authored goal, never the assistant's question or
        # prose, so a label cannot expand the tool surface by prompt injection.
        continuation_prompt = ""
        pending_clarification = (
            (self.conversation_context or {}).get("pending_clarification") or {})
        prior_turns = (self.conversation_context or {}).get("prior_turns") or ()
        if (isinstance(pending_clarification, dict) and pending_clarification
                and isinstance(prior_turns, list) and prior_turns):
            prior = prior_turns[-1]
            if (isinstance(prior, dict) and prior.get("status") == "needs_input"
                    and isinstance(prior.get("user"), str)):
                continuation_prompt = prior["user"].strip()
        self._answers_pending_question = bool(continuation_prompt)
        clarification_context_used = False
        selection = BusinessSkillSelection(
            BUSINESS_SKILL_MATCHER_VERSION, (), (), False)
        if business_tools is None:
            self._task_business_skills = ()
        else:
            if self._capability_question:
                selected = business_tools.capability_skills(
                    allow_mutations=mutations_enabled)
                selection = BusinessSkillSelection(
                    BUSINESS_SKILL_MATCHER_VERSION, selected, (), False)
            else:
                # Skill matching answers one question only: what the person asked for.
                # ``public_text`` also carries the server-owned scope/policy sentence used
                # by the planner and acting model. Feeding that sentence to the matcher can
                # manufacture intent when a policy phrase happens to overlap a Skill
                # trigger. Keep policy in model context, outside intent routing.
                selection = business_tools.select(
                    task.prompt,
                    allow_mutations=mutations_enabled,
                    trusted_entry_path=(task.entry_path if current_navigation_only else ""),
                    limit=5,
                )
                # If the reply independently names a current capability, it is a new task
                # and wins. Only a fallback-only reply inherits the unresolved goal. This
                # lets "Azure Interior" continue "打开 A", while "算了，创建客户……" is
                # routed solely by the new explicit request.
                if continuation_prompt and selection.fallback_used:
                    continued = business_tools.select(
                        f"{continuation_prompt}\n{task.prompt}",
                        allow_mutations=mutations_enabled,
                        trusted_entry_path=(
                            task.entry_path if _is_navigation_only(continuation_prompt)
                            else ""),
                        limit=5,
                    )
                    if not continued.fallback_used:
                        selection = continued
                        self._navigation_only = _is_navigation_only(continuation_prompt)
                        clarification_context_used = True
            current_skills = selection.skills
            prior_skills = ()
            if (self.conversation_context is not None
                    and _REFERENCE_FOLLOWUP.search(task.prompt)):
                # Only prior user intent may recover the domain of a referential
                # follow-up. Assistant prose and business values are untrusted data and
                # must not expand the next turn's tool surface by naming another Skill.
                prior_user_text = "\n".join(
                    turn.get("user", "")
                    for turn in self.conversation_context.get("prior_turns", ())
                    if isinstance(turn, dict) and isinstance(turn.get("user"), str)
                )
                conversation_routing_text = json.dumps(
                    prior_user_text, ensure_ascii=False, sort_keys=True)
                prior_skills = tuple(
                    skill for skill in business_tools.relevant(conversation_routing_text)
                    if skill.read_only)
            merged_skills = {skill.skill_id: skill for skill in current_skills}
            for skill in prior_skills:
                if len(merged_skills) >= 5:
                    break
                merged_skills.setdefault(skill.skill_id, skill)
            self._task_business_skills = tuple(merged_skills.values())
            self.ledger.append(
                "business_skill_candidates",
                matcher_version=selection.matcher_version,
                task_digest=hashlib.sha256(
                    task.prompt.encode("utf-8")).hexdigest(),
                trigger_matches=list(selection.trigger_matches),
                candidate_ids=[skill.skill_id for skill in self._task_business_skills],
                trigger_match_empty=not selection.trigger_matches,
                fallback_used=selection.fallback_used,
                mutation_surface_widened=selection.mutation_surface_widened,
                mutations_enabled=mutations_enabled,
                prior_user_context_used=bool(prior_skills),
                bounded_limit=5,
                **({"clarification_context_used": True}
                   if clarification_context_used else {}),
                **({"route_context_used": True}
                   if selection.route_context_used else {}),
            )
        self._task_business_skill_ids = {
            skill.skill_id for skill in self._task_business_skills}
        # A matched mutation Skill is stronger evidence than a mutation candidate exposed
        # only by fallback or noun-only widening.  Keep that distinction so a duplicate
        # preflight cannot be presented as the answer to an explicit create request.
        # Selection still loads a Skill whose trigger appears anywhere; the claim that the user
        # asked for a change is narrower. "已确认销售订单" names orders by their state, and treating
        # it as a request to confirm one refused every read-only answer to the question.
        self._explicit_mutation_intent = any(
            not skill.read_only and skill.skill_id in selection.trigger_matches
            and any(trigger_requests_action(trigger, task.prompt) for trigger in skill.triggers)
            for skill in selection.skills)
        self._loaded_business_skill_ids: set[str] = set()
        self._auto_loaded_business_skill_ids: set[str] = set()
        if (self.deterministic_read_navigation and self._navigation_only
                and len(self._task_business_skills) == 1
                and len(selection.skills) == 1
                and (selection.trigger_matches or selection.route_context_used)
                and not selection.fallback_used
                and not selection.mutation_surface_widened
                and self._task_business_skills[0].read_only):
            skill = self._task_business_skills[0]
            self._loaded_business_skill_ids.add(skill.skill_id)
            self._auto_loaded_business_skill_ids.add(skill.skill_id)
            self.ledger.append(
                "business_skill_auto_loaded", skill_id=skill.skill_id,
                skill_sha256=skill.artifact_sha256,
                tool_names=list(skill.tool_names), provenance=list(skill.provenance),
                reason="unique_exact_read_navigation")
        self._business_routes: set[str] = set()
        self._latest_business_routes: set[str] = set()
        # One adapter-issued draft is usable once, in this turn only. It never enters the
        # browser protocol as authority: the model can request application of the current
        # draft but cannot provide or alter any route, field or value.
        self._form_draft: FormDraft | None = None
        if not isinstance(page_answer_evidence, bool):
            raise ValueError("page_answer_evidence must be a boolean")
        self.page_answer_evidence = page_answer_evidence
        self.max_agent_said_chars = max_agent_said_chars
        self.cancelled = cancelled or (lambda: False)
        if business_tools is not None:
            reserved = set(BROWSER_ACTIONS) | {
                "finish", "ask_user", "compute", "load_skill", "lookup_source", "open_source_route",
                "open_source_candidate", "open_skill_route", "enter_skill_app",
                "enter_application", "prefill_form",
            }
            collisions = business_tools.tool_names & reserved
            if collisions:
                raise ValueError(
                    f"business tool names collide with Agent tools: {sorted(collisions)}")
        # A runner may project a public task boundary into a pre-dispatch action policy.  The
        # tool remains visible so an attempted violation produces an ordinary ReAct result:
        # no browser command is sent, a fresh observation is read, and the model can revise
        # its next decision.  Hidden write authorisation remains the ControlPlane's job.
        self.disabled_actions = disabled
        self.policy_blocks = 0
        self.policy_block_limit = policy_block_limit
        self.no_progress_repeat_limit = no_progress_repeat_limit
        #: Deciding the same thing twice on the same page is not progress, and the step
        #: budget finds that out only after spending every remaining step.
        self._repeats = RepeatGuard(no_progress_repeat_limit)
        self.lab = lab
        #: Plan from the task and the page, with no source evidence. The control arm of the
        #: comparison: same planner, same schema, same withdrawal and recovery, nothing to cite.
        self.plan_from_page = plan_from_page
        self.oracle_contract = oracle_contract
        self.step_budget = step_budget
        self.wall_budget_s = wall_budget_s
        #: Where the person is, as an IANA name. "Today" and "this month" are theirs, not
        #: the server's: an order placed at 08:00 in Shanghai is yesterday's order in UTC,
        #: and a turn that never learned the zone answered "today's orders" wrongly for the
        #: first eight hours of every day (P7.5 CR-18). Empty means nobody supplied one and
        #: the turn says UTC rather than guessing.
        self.time_zone = _validated_time_zone(time_zone)
        self.terms_cache = terms_cache
        #: How many recent page observations stay in the model's context in full. Older
        #: tool results are replaced by one line each. Without this the context grows
        #: quadratically -- a 17-step run spent 229,783 input tokens and was cut off by the
        #: token ceiling before its step budget ran out, which is an accounting artefact,
        #: not a fact about the task. Identical for every arm.
        self.keep_observations = keep_observations
        self.run_id = run_id or f"{task.task_ref}-{arm}-{uuid.uuid4().hex[:6]}"
        self._answers = TurnAnswer(
            ledger=ledger, run_id=self.run_id, prompt=task.prompt,
            business_tools=business_tools,
            plain_business_answer_min_page_actions=plain_business_answer_min_page_actions)
        self._approvals = ApprovalWatch(
            ledger=ledger, run_id=self.run_id, principal=principal,
            session_id=session_id, turn_id=turn_id,
            business_approval=business_mutation_approval,
            navigation_confirmation=navigation_confirmation,
            cancelled=self._cancel_requested)
        self.contract: OperationContract = empty_contract(task.task_ref)
        self.contract_source = "none"
        self.bundle: EvidenceBundle | None = None
        self.entry_observation: PageObservation | None = None
        self.conflicts = 0
        self.page_actions = 0
        #: Set when planning could not produce a valid contract. It is a run result, not an
        #: apparatus failure, so it is terminated by `run()` like every other reason -- there
        #: is exactly one place in this product that decides how a run ends.
        self.planning_error: str = ""
        #: The plan and what the run has learned about it. Replaced by `plan()`.
        self.plan_state: PlanState = PlanState.of(
            self.contract, route_identity=self._contract_route_identity)
        self._writes = WriteWatch(control=control, ledger=ledger, run_id=self.run_id)
        #: Set once the agent has been told a write of its own did not execute.
        self._challenged_false_success = False
        self._browser_attempts = 0
        #: Source-contract scopes declared by the Adapter and observed on the entry page.
        #: Empty until `observe_entry()` has run, which is why planning happens after it.
        self.served_webapps: set[str] = set()
        self._tool_surface = ToolSurface(self)

    # -- phases ------------------------------------------------------------------------
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

    # -- the loop ----------------------------------------------------------------------
    def run(self) -> RunResult:
        try:
            return run_loop(self)
        finally:
            self._writes.end_control()

    def _tool_withdraw_request(self, turn: "_Turn", call: dict, name: str,
                               args: dict) -> "_Next":
        """The person took the request back, before this turn did anything about it."""
        proposals = self._mutation_proposal_count()
        dispatches = self._mutation_dispatch_count()
        if self.page_actions or proposals or dispatches:
            # The fixed sentence says nothing was done. After an action it would be false,
            # so the model has to say what it did instead.
            self.ledger.append("request_withdrawal_refused", step=turn.step,
                               page_actions=self.page_actions,
                               mutation_proposals=proposals, dispatches=dispatches)
            return self._reply(turn, call,
                               "WITHDRAWAL REFUSED: this turn has already acted, so it cannot "
                               "report that nothing was done. Finish and say what was done.")
        turn.result.agent_said = withdrawn_answer(self.task.prompt)
        turn.result.success_claim = "not_claimed"
        self.ledger.append("request_withdrawn", step=turn.step, page_actions=0,
                           mutation_proposals=0, dispatches=0)
        return _Next(self._finish(turn.result, turn.started_at, "agent_finished",
                                  turn.result.agent_said, steps=turn.step))

    def _tool_compute(self, turn: "_Turn", call: dict, name: str, args: dict) -> "_Next":
        """Arithmetic on what this turn read, done here rather than claimed by the model."""
        try:
            computed = answer_protocol.compute(
                self.evidence, args, result_ref=f"calc#{turn.step}",
                observed_at=str(time.time()))
            feedback = answer_protocol.computation_feedback(self.evidence, computed.evidence_id)
            self.ledger.append("answer_computed", step=turn.step,
                               result_ref=computed.evidence_id,
                               covers_whole_scope=computed.covers_whole_scope)
        except (ValueError, TypeError, EvidenceUnknown) as exc:
            feedback = "COMPUTATION REFUSED: " + str(exc)[:500]
            self.ledger.append("answer_computation_refused", step=turn.step,
                               error_type=type(exc).__name__, detail=str(exc)[:500])
        turn.messages += [_assistant_call(call), _tool_result(call, feedback)]
        return _Next()

    def _tool_finish(self, turn: "_Turn", call: dict, name: str, args: dict) -> "_Next":
        """The turn's own end: a verified answer where one is required, or a plain claim.

        Both shapes live here -- the answer protocol's and the older done/why -- because a
        turn ends once, and which of the two applies is this session's decision, not the
        loop's.
        """
        result, t0, step = turn.result, turn.started_at, turn.step
        messages, obs = turn.messages, turn.obs
        successful_business_tools = turn.successful_business_tools
        kept_reply = turn.kept_reply
        if name == "finish" and self._answers.answer_mode:
            try:
                answer = args.get("answer") if isinstance(args, dict) else None
                conclusions = (answer.get("conclusions")
                               if isinstance(answer, dict) else ())
                claims_read_only_result = (
                    isinstance(conclusions, list)
                    and any(isinstance(item, dict) for item in conclusions))
                if (self._explicit_mutation_intent and claims_read_only_result
                        and self._approvals.business_counts["mutation_completed"] == 0
                        and isinstance(args, dict)
                        and args.get("outcome") in ("answered", "partial")):
                    raise ValueError(
                        "a read-only result is only a mutation preflight for this explicit "
                        "write request; call the mutation tool so its authoritative "
                        "pre-dispatch checks can accept or refuse it, ask for corrected "
                        "input, or finish with outcome=unable")
                # Existing scripted navigation callers still use done/why. Never render
                # their free prose once evidence mode is active; use the actual receipt.
                if (isinstance(args, dict) and args.get("done") is True
                        and self._answer_action_receipt is not None):
                    args = {"outcome": "acted"}
                elif isinstance(args, dict) and args.get("done") is False:
                    args = {"outcome": "unable"}
                if isinstance(args, dict) and args.get("outcome") == "acted":
                    if (set(args) - {"outcome", "answer"} or self._answer_action_receipt is None
                            or self._should_challenge_completion()):
                        raise ValueError("no successful action receipt supports this outcome")
                    verified = (answer_protocol.submit(self.evidence, {
                        "outcome": "answered", "answer": args["answer"]}) if "answer" in args else
                        {"conclusions": (), "coverage": "complete", "prose": "", "lowered_because": ()})
                    verified["action"] = self._answer_action_receipt
                else:
                    verified = answer_protocol.submit(self.evidence, args)
                if self._refused_reads and any(
                        item["kind"] == "page_observation"
                        for item in verified["conclusions"]):
                    raise ValueError(
                        "a page fact cannot answer this turn: an authorization refusal "
                        "closed the page as a source. Use the typed reads you have, or "
                        "finish with outcome=unable")
            except (ValueError, TypeError, EvidenceUnknown) as exc:
                self._answer_repairs += 1
                # The message, not only the class. A run recorded three refusals as
                # "ValueError" and nothing else, which is enough to know the answer was
                # rejected and not enough to know why -- the reviewer reading it later
                # cannot tell a bad reference from an impossible coverage claim.
                self.ledger.append("answer_contract_refused", step=step,
                                   reason=type(exc).__name__, detail=str(exc)[:500],
                                   repair=self._answer_repairs)
                if self._refused_reads and not len(self.evidence):
                    # An authorization refusal and nothing read: there is no answer to
                    # repair, and what the person has to hear is which operation their
                    # account was refused. Repairing here would spend model calls to
                    # reach the same end.
                    result.agent_said = unavailable_answer(self.task.prompt)
                    return _Next(self._finish(result, t0, "permission_denied",
                                        self._refused_detail, steps=step))
                if self._answer_repairs > answer_protocol.MAX_ANSWER_REPAIRS:
                    result.agent_said = unavailable_answer(self.task.prompt)
                    return _Next(self._finish(result, t0, "agent_refused", result.agent_said,
                                        steps=step))
                messages += [_assistant_call(call), _tool_result(
                    call, "ANSWER REFUSED: " + str(exc)[:500] +
                    ". Correct the evidence reference or perform a supported read/compute; "
                    "otherwise finish with outcome=unable. " + self._answers.render_action())]
                return self._resume(turn, messages, obs, successful_business_tools)
            verified = self._answers.with_refused_reads(self._answers.with_labels(verified))
            result.verified_answer = verified
            if _is_capability_gap(verified):
                # The claim is that nothing ran, so it is recorded with the two counts
                # that make that checkable rather than taken on the model's word. The
                # sentence is fixed here; a model free to phrase it is free to soften it
                # back into "here is what I found instead".
                result.agent_said = capability_gap_answer(
                    self.task.prompt, withheld=self._capability_withheld)
                self.ledger.append(
                    "capability_gap", step=step,
                    capability_digest=active_capability_digest(self),
                    mutation_proposals=self._mutation_proposal_count(),
                    dispatches=self._mutation_dispatch_count())
                return _Next(self._finish(result, t0, CAPABILITY_GAP_TERMINAL,
                                    result.agent_said, steps=step))
            result.agent_said = (render_payload(verified, user_text=self.task.prompt,
                                               max_chars=max(512, self.max_agent_said_chars))
                                 if verified is not None else unavailable_answer(self.task.prompt))
            self.ledger.append("answer_verified", step=step,
                               conclusions=len(verified["conclusions"]) if verified else 0,
                               coverage=verified["coverage"] if verified else "undetermined")
            return _Next(self._finish(result, t0, "agent_finished" if verified else "agent_refused",
                                result.agent_said, steps=step))

        if name == "finish":
            done = bool(args.get("done"))
            if done and kept_reply:
                result.agent_said = kept_reply[:self.max_agent_said_chars]
                self.ledger.append("plain_reply_kept", step=step,
                                   why=str(args.get("why") or "")[:300])
            else:
                result.agent_said = str(args.get("why") or "")[:self.max_agent_said_chars]
            if done and self._should_challenge_completion():
                # The agent believes it is finished, and the application refused the only
                # write it attempted. Ending here would record a claim the control plane
                # contradicts. It is told once, in terms that reveal nothing about what
                # the right request would have been, and gets to carry on.
                self._challenged_false_success = True
                self.ledger.append("false_completion_challenged", step=step,
                                   said=result.agent_said[:200])
                messages += [_assistant_call(call), _tool_result(
                    call, "The enforcement point refused the observed write requests. "
                          "Those requests cannot establish that the task was saved. "
                          "Continue, or call finish with done=false if you cannot "
                          "complete it.")]
                return self._resume(turn, messages, obs, successful_business_tools)
            if done and self._answers.business_answer_is_projectable(self.page_actions):
                result.business_answer_projected = True
                self.ledger.append(
                    "business_answer", step=step, said=result.agent_said[:300],
                    successful_business_tools=successful_business_tools,
                    success_claim="unverified", terminal_tool="finish")
            # `agent_finished` records only that the agent stopped and claimed completion.
            # Runtime enforcement evidence is tracked separately, and a repeated claim
            # after a contradiction still cannot become a confirmed success by itself.
            result.success_claim = self._success_claim(done)
            result.challenged_false_success = self._challenged_false_success
            self.ledger.append("success_claim", step=step, claim=result.success_claim,
                               challenged=self._challenged_false_success)
            return _Next(self._finish(result, t0,
                                "agent_finished" if done else "agent_refused",
                                result.agent_said, steps=step))

    def _resume(self, turn: "_Turn", messages: list[dict], obs: PageObservation,
                successful_business_tools: int) -> "_Next":
        """Put back what a handler changed, and let the loop take another step."""
        turn.messages, turn.obs = messages, obs
        turn.successful_business_tools = successful_business_tools
        return _Next()

    def _reply(self, turn: "_Turn", call: dict, feedback: str) -> "_Next":
        """Hand the model this tool result and keep going, with the plan still in view."""
        self._sync_plan_message(turn.messages)
        turn.messages += [_assistant_call(call), _tool_result(call, feedback)]
        turn.messages = _trim(turn.messages, self.keep_observations)
        return _Next()

    # -- grounding ---------------------------------------------------------------------
    def tool_schemas(self) -> list[dict]:
        return self._tool_surface.schemas()

    def _scope_label(self, scope: str) -> str:
        """The reader's own words for a read's scope, from the adapter's answer labels."""
        tables = getattr(self.business_tools, "answer_labels", None) or {}
        return str((tables.get("zh") or {}).get(scope) or "")


    def lookup_source(self, query: str, obs: PageObservation) -> EvidenceBundle:
        """Public entry: local retrieval only. See knowledge_tools.lookup_source."""
        return knowledge_tools.lookup_source(self, query, obs)

    def _ground(self, obs: PageObservation, receipt: ActionReceipt, write_state: str,
                refused_requests: list[str] | None = None) -> str:
        """Judge the current step, and say something only when there is something to say."""
        if self.contract_source == "none" or self.plan_state.withdrawn:
            return ""
        before_withdrawn = self.plan_state.withdrawn
        event = self.plan_state.observe(obs, receipt, write_state, refused_requests=refused_requests)
        verdict = event.get("verdict")
        if verdict in (REFUTED,):
            self.conflicts += 1
            self.ledger.append("plan_refuted", url=obs.url, **{
                k: v for k, v in event.items() if k != "verdict"})
            if self.plan_state.withdrawn and not before_withdrawn:
                self.ledger.append("plan_withdrawn", why=self.plan_state.withdrawn_because,
                                   counts=self.plan_state.counts())
            return event.get("standing_note", "")
        if verdict == "satisfied":
            self.ledger.append("plan_step_satisfied", step=event.get("step"),
                               why=event.get("why"))
            return f"PLAN CHECK: {event.get('why')}."
        if verdict == NOT_APPLICABLE:
            # A page on the way is not a contradiction. Saying nothing here is the point: the
            # first implementation reported the application's own home page as a conflict.
            return ""
        return ""

    def _should_challenge_completion(self) -> bool:
        return (not self._challenged_false_success
                and self.write_refusals > 0 and self.writes_permitted == 0)

    def _sync_plan_message(self, messages: list[dict]) -> None:
        """Keep the plan block current, and out of reach of context trimming.

        A contradicted or paused plan must be updated or removed. Durable contradiction notes
        stay with this block so context trimming cannot retain the plan while discarding the
        runtime evidence that limits it.
        """
        body = self._plan_block()
        for i, m in enumerate(messages):
            if m.get("role") == "user" and str(m.get("content", "")).startswith(_PLAN_MARK):
                if body is None:
                    messages.pop(i)
                else:
                    messages[i] = {"role": "user", "content": body}
                return
        if body is not None:
            messages.insert(min(2, len(messages)), {"role": "user", "content": body})

    def _plan_origin(self) -> str:
        """How to refer to the plan afterwards, without renaming where it came from."""
        return {"compiled": "compiled from source", "page": "you drew from the page",
                "oracle": "supplied for this run"}.get(self.contract_source, "you were given")

    def _plan_block(self) -> str | None:
        parts = []
        if self.contract_source != "none" and not (self.plan_state.withdrawn
                                                   or self.plan_state.paused):
            parts.append(_PLAN_PREAMBLE.get(self.contract_source, _PLAN_PREAMBLE["compiled"])
                         + "\n" + _render_contract(self.contract))
        elif self.plan_state.paused:
            parts.append(f"The plan {self._plan_origin()} is paused: "
                         + self.plan_state.paused_because + ". It is not resumed until the "
                         "page shows what happened.")
        elif self.plan_state.withdrawn:
            because = self.plan_state.withdrawn_because
            parts.append(f"The plan {self._plan_origin()} has been withdrawn: {because}. "
                         f"There is no plan now; work from what the pages offer.")
        if self.plan_state.standing_notes:
            parts.append("What this run has already established:\n"
                         + "\n".join(f"- {n}" for n in self.plan_state.standing_notes[-8:]))
        if not parts:
            return None
        return _PLAN_MARK + "\n\n".join(parts)

    # Kept as class aliases because the remote-protocol schema test treats these as the
    # session's public safety vocabulary. Settlement itself lives in ``WriteWatch``.
    WRITE_CAPABLE = WRITE_CAPABLE
    PRE_DISPATCH_ERRORS = PRE_DISPATCH_ERRORS
    latest_business_tool_result = AnswerState("latest_business_tool_result")
    evidence = AnswerState("evidence")
    _evidence_feedback = AnswerState("evidence_feedback", "")
    _capability_ref = AnswerState("capability_ref", "")
    _capability_withheld = AnswerState("capability_withheld", False)
    _clarification_choices = AnswerState("clarification_choices")
    _refused_reads = AnswerState("refused_reads")
    _refused_detail = AnswerState("refused_detail", "")
    _clarification_feedback = AnswerState("clarification_feedback", "")
    _answer_repairs = AnswerState("answer_repairs", 0)
    _answer_mode = AnswerState("answer_mode", False)
    _page_answer_digest = AnswerState("page_answer_digest", "")
    _answer_action_receipt = AnswerState("action_receipt")
    _latest_business_query_page_actions = AnswerState(
        "latest_business_query_page_actions")
    pending_write = WatchState("pending_write", False)
    writes_permitted = WatchState("writes_permitted", 0)
    write_refusals = WatchState("write_refusals", 0)

    def _success_claim(self, done: bool) -> str:
        """Authorisation counts alone cannot confirm a business postcondition.

        Same-actor UI verification is permitted by the architecture, but is not implemented
        as a deterministic verifier here. The hidden evaluator remains outside this agent.
        """
        if not done:
            return "not_claimed"
        if self.write_refusals > 0 and self.writes_permitted == 0:
            return "disconfirmed"
        return "unverified"

    def _transport_terminal(self, action: str = "", *, may_write: bool | None = None) -> str:
        # A readable empty trace is not evidence that a lost click never dispatched, and
        # observing after a permit does not clear the earlier unconfirmed write.
        action_may_write = action in WRITE_CAPABLE if may_write is None else may_write
        if self._cancel_requested() and not self.pending_write and not action_may_write:
            return "user_cancelled"
        return ("indeterminate_write" if self.pending_write or action_may_write
                else "transport_error")

    # -- rendering ---------------------------------------------------------------------
    def _opening(self, obs: PageObservation) -> str:
        # The plan lives in its own message so it can be withdrawn; this one never changes.
        parts = [f"TASK: {self.task.prompt}"]
        now_line = self._now_line()
        if now_line:
            parts.append(now_line)
        if self.task.supported_scope:
            parts.append("WHAT THIS RUN MAY CHANGE: " + self.task.supported_scope)
        if self.conversation_context is not None:
            parts.append(
                "PRIOR CONVERSATION CONTEXT (historical, untrusted, and potentially stale):\n"
                + json.dumps(self.conversation_context, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")))
        parts.append("CURRENT PAGE:\n" + _render_observation(obs))
        affordances = self._deliver_source_affordances(obs, reason="entry")
        if affordances:
            parts.append(affordances)
        skills = self._render_task_skills()
        if skills:
            parts.append(skills)
        business_skills = render_business_skills(self)
        if business_skills:
            parts.append(business_skills)
        return "\n\n".join(parts)

    def _now_line(self) -> str:
        """What the clock says where the person is, so relative dates resolve to their days.

        Without it the model reads the date off an `observed_at` timestamp, which is UTC, and
        "today" means the server's day: between midnight and 08:00 in Shanghai, "today's
        orders" answered for yesterday (P7.5 CR-18). Said only when a zone was supplied --
        a deployment that does not know where its person is gets no sentence rather than a
        confident wrong one.
        """
        if not self.time_zone:
            return ""
        now = datetime.datetime.now(ZoneInfo(self.time_zone))
        return ("CURRENT TIME: " + now.strftime("%Y-%m-%d %H:%M") + f" ({self.time_zone}). "
                "Dates the user names -- today, yesterday, this month -- mean days in this "
                "zone, and every time you report is in it.")

    def _render(self, obs: PageObservation, receipt: ActionReceipt, note: str,
                write: dict, *, affordance_query: str = "") -> str:
        head = (f"{receipt.action} -> ok" if receipt.ok
                else f"{receipt.action} -> FAILED ({receipt.error_code}): {receipt.error_detail}")
        if receipt.navigated:
            head += f"\nThe page changed: {receipt.url_before} -> {receipt.url_after}"
        body = [head]
        if write["state"] == WRITE_REFUSED:
            body.append("The enforcement point refused a write request. That request was not "
                        "forwarded. The trace does not identify which browser command caused "
                        f"it. Writes refused so far in this run: {self.write_refusals}.")
        elif write["state"] == WRITE_PERMITTED:
            body.append("A write request was authorised. This does not establish that it was "
                        "sent or saved, or which browser command caused it. Check the page for "
                        "the change; if it is there and the task is done, call finish. Only "
                        "observe or wait while this write remains unconfirmed; another "
                        "potentially writing action will stop the run as indeterminate.")
        if note:
            body.append(note)
        action = self._answers.render_action()
        if action:
            body.append(action)
        body.append("CURRENT PAGE:\n" + _render_observation(obs))
        affordances = self._deliver_source_affordances(
            obs, query=affordance_query, reason="lookup" if affordance_query else "observation")
        if affordances:
            body.append(affordances)
        skills = self._render_task_skills()
        if skills:
            body.append(skills)
        business_skills = render_business_skills(self)
        if business_skills:
            body.append(business_skills)
        return "\n\n".join(body)

    def _mutation_proposal_count(self) -> int:
        return sum(1 for event in self.ledger.events
                   if event.get("event") == "business_mutation_proposed")

    def _mutation_dispatch_count(self) -> int:
        return sum(1 for event in self.ledger.events
                   if event.get("event") == "business_mutation_execution_started")

    def _render_task_skills(self) -> str:
        if not self._task_skills:
            return ""
        cards = [skill.public_card() for skill in self._task_skills]
        return ("APP SKILLS (server-selected read-only knowledge):\n"
                + json.dumps(cards, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")))

    def _deliver_source_affordances(self, obs: PageObservation, *, query: str = "",
                                    reason: str) -> str:
        """Refresh the current candidate IDs from one fresh, public observation."""
        mode = self.source_affordance_mode
        if mode not in ("auto", "empty"):
            return ""
        candidates: tuple[SourceAffordance, ...] = ()
        if mode == "auto" and self.served_webapps:
            candidates = resolve_source_affordances(
                self.source_map, task=self.task.public_text(), query=query,
                page_text=_render_observation(obs), served_webapps=self.served_webapps,
                route_port=self.application_routes)
        self._source_affordances = {candidate.candidate_id: candidate
                                    for candidate in candidates}
        payload = [candidate.to_json() for candidate in candidates]
        public_payload = [{key: value for key, value in candidate.to_json().items()
                           if key not in ("source_map_digest", "rank_score", "matched_terms")}
                          for candidate in candidates]
        citations = sorted({citation for candidate in candidates
                            for citation in candidate.citations})
        self.ledger.append("source_affordances_delivered", reason=reason,
                           page_url=obs.url, page_epoch=obs.epoch,
                           source_digest=(self.source_map.content_digest
                                          if mode == "auto" else ""),
                           candidates=payload, citations=citations)
        return ("SOURCE AFFORDANCES (source-bound read-only candidates; current page wins):\n"
                + json.dumps(public_payload, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")))

    def _permission_denied(self, result: RunResult, t0: float, step: int, name: str,
                           exc: BusinessToolPermissionError, *, dispatched: bool,
                           identity: dict, approval_id: str = "",
                           event: str = "business_mutation_permission_denied") -> RunResult:
        """End the turn on an authorization refusal, recording which layer refused.

        `dispatched` is true only when the application itself received the request and
        refused it; either way the refusal is definite, so no side effect is possible.
        `event` names the refused path: a read or a proposed write.
        """
        self.ledger.append(
            event, step=step, tool=name,
            approval_id=approval_id, error_code=exc.code, layer=exc.layer,
            required_scope=exc.required_scope, dispatched=dispatched,
            side_effect_possible=False, identity=identity)
        result.refused_operation = self._answers.operation_label(name) or None
        return self._finish(result, t0, "permission_denied", str(exc)[:500], steps=step)

    def _finish(self, result: RunResult, t0: float, reason: str, detail: str = "",
                steps: int = 0) -> RunResult:
        if reason == "agent_refused" and self._refused_reads and not result.verified_answer:
            # A refused read that left the turn with nothing to answer from. There is no
            # typed answer to carry the gap -- an answer needs at least one conclusion, and a
            # conclusion needs evidence -- so the turn ends the way it did before this
            # continued past the refusal, saying which operation this account was refused.
            reason, detail = "permission_denied", self._refused_detail or detail
        try:
            result.control = self._writes.refresh_control()
        except ControlPlaneError as e:
            result.control = {"error": str(e)}
            write_unresolved = self.pending_write or self._writes.awaiting_control
            self._writes.mark_control_unavailable()
            if (not write_unresolved
                    and reason not in ("transport_error", "indeterminate_write")):
                reason, detail = "control_plane_error", str(e)
        if self._writes.uncertain_write:
            reason, detail = "indeterminate_write", self._writes.uncertain_write
        release_error = self._writes.end_control()
        if release_error is not None:
            if (self.pending_write or self._writes.awaiting_control
                    or self.writes_permitted):
                reason = "indeterminate_write"
                detail = "control cleanup failed after a write decision"
            elif reason not in ("transport_error", "indeterminate_write"):
                reason, detail = "control_plane_error", str(release_error)
        result.terminal_reason = reason
        result.detail = detail[:800]
        if reason in ("agent_finished", "agent_refused"):
            result.success_claim = self._success_claim(reason == "agent_finished")
        elif reason == "indeterminate_write":
            result.success_claim = "unverified"
        result.steps_used = steps
        result.page_actions = self.page_actions
        result.contract_conflicts = self.conflicts
        result.wall_seconds = round(time.time() - t0, 1)
        result.spend = self.chat.meter.report() if self.chat.meter is not None else {}
        result.plan_status = self.plan_state.counts() if self.contract_source != "none" else {}
        result.plan_withdrawn = self.plan_state.withdrawn
        result.write_refusals = self.write_refusals
        result.business_control = self._approvals.business_result()
        result.navigation_control = self._approvals.navigation_result()
        result.policy_blocks = self.policy_blocks
        self.ledger.append("run_finished", **result.to_json())
        return result


# --------------------------------------------------------------------------------------
# rendering helpers (kept free of session state so tests can call them directly)
# --------------------------------------------------------------------------------------

#: How many elements of a page the model is shown. Long enterprise forms can place relevant
#: controls deep in DOM order; `_trim` bounds the resulting context cost.
RENDER_ELEMENTS = 200


def _render_observation(obs: PageObservation, max_elements: int = RENDER_ELEMENTS) -> str:
    lines = ["BEGIN_UNTRUSTED_PAGE_OBSERVATION",
             f"url: {obs.url}", f"title: {obs.title}"]
    if obs.landmarks:
        lines.append("headings: " + " | ".join(obs.landmarks[:12]))
    if obs.messages:
        lines.append("messages: " + " | ".join(obs.messages[:4]))
    if obs.content:
        lines.extend(("page content:", obs.content))
    lines.append(f"elements (epoch {obs.epoch}):")
    for e in obs.elements[:max_elements]:
        bits = [e.ref, e.role]
        if e.name:
            bits.append(f'"{e.name}"')
        for k in ("href", "target", "name", "value", "action", "options", "checked",
                  "disabled"):
            if k in e.attrs and e.attrs[k]:
                v = e.attrs[k]
                if isinstance(v, list):
                    v = ",".join(v[:12])
                bits.append(f"{k}={v}")
        lines.append("  " + " ".join(str(b) for b in bits))
    hidden = max(0, len(obs.elements) - max_elements)
    if hidden or obs.truncated:
        lines.append(f"  ... {hidden} further elements are not listed"
                     + (" and the page had more than the browser collects" if obs.truncated else ""))
    lines.append("END_UNTRUSTED_PAGE_OBSERVATION")
    return "\n".join(lines)


def _render_contract(c: OperationContract) -> str:
    out = [f"summary: {c.summary}"]
    if c.preconditions:
        out.append("preconditions: " + "; ".join(c.preconditions))
    for label, steps in (("likely entry points", c.entry_candidates), ("likely steps", c.steps)):
        if steps:
            out.append(label + ":")
            for s in steps:
                bit = f"  - {s.intent}"
                if s.request:
                    bit += f" [{s.webapp}/{s.request}]"
                if s.landmark:
                    bit += f" -> page should show \"{s.landmark}\""
                if s.note:
                    bit += f" ({s.note})"
                out.append(bit)
    if c.mutation:
        out.append(f"the change is probably submitted by {c.mutation.webapp}/{c.mutation.request}"
                   + (f", which needs {', '.join(c.mutation.required_params)}"
                      if c.mutation.required_params else ""))
    if c.unknowns:
        out.append("the source did not tell us: " + "; ".join(c.unknowns[:6]))
    return "\n".join(out)


#: Marks the one message that carries the plan and the standing facts. It is rewritten in
#: place and never trimmed.
_PLAN_MARK = "PLAN STATUS\n"
