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

from ..composition.ports import ApplicationRoutePort
from ..composition.sources import SourceCapability
from ..answer import answer_protocol
from ..business.approvals import ApprovalWatch, NavigationPlan
from ..answer.answer_view import (capability_gap_answer, render_payload, unavailable_answer,
                          withdrawn_answer)
from ..planning.affordance import SourceAffordance, resolve_source_affordances
from ..transport.browser import BrowserExecutor
from ..business.runtime import (BUSINESS_SKILL_MATCHER_VERSION, BusinessSkill,
                               BusinessSkillSelection, FormDraft, BusinessToolRuntime)
from ..business.forms import ApplicationFormPort, ApplicationFormTools
from ..business.errors import BusinessToolPermissionError
from ..business.text import trigger_requests_action
from ..planning.contract import (ContractPlanner, PlanningFailed, _segment_from_template,
                                expand_terms)
from ..planning.retrieval import EvidenceBundle, SourceRetriever
from ..business.controlplane import ControlPlaneClient, ControlPlaneError
from ..modeling.egress import AuditedChat, ProviderEgressError
from .grounding import (NOT_APPLICABLE, REFUTED, WRITE_PERMITTED, WRITE_REFUSED,
                        PlanState, opaque_route_identity)
from ..state.identity import PrincipalContext
from ..planning.knowledge import ApplicationKnowledgePort
from ..state.ledger import SessionLedger
from ..privacy import SessionHandoffPolicy, safe_tool_arguments as _safe
from .routes import Router, candidate_url
from .turnwatch import ProgressFeed, RepeatGuard, SourcePageProgress
from .turn_answer import AnswerState, TurnAnswer
from .tools import ToolSurface
from .business_tools import render_business_skills
from . import knowledge_tools
from .knowledge_tools import active_capability_digest
from .loop import run_loop
from .finish import FinishHandler
from .context import (RENDER_ELEMENTS, SessionContext, _PLAN_MARK,
                      _render_contract, _render_observation)
from .lifecycle import SessionLifecycle
from .runstate import (CAPABILITY_GAP_TERMINAL, LabApparatus, NO_LAB, RunResult,
                       TaskSpec, _Next, _Turn)
from .click_policy import ClickPolicy
from .target import (MAX_TARGET_DECLARATIONS, TargetState, describe_target, parse_target,
                     required_page_controls, required_page_effects, unmet_conditions)
from ..business.writes import PRE_DISPATCH_ERRORS, WatchState, WriteWatch
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


class ServerAgentSession(SessionLifecycle, FinishHandler, SessionContext):
    """One task, one browser, one model, one audit ledger.

    The constructor takes three kinds of thing, and the signature says which is which --
    confusing them is how an experiment leaks into a product:

    * **`source`** -- a `SourceCapability`: what this deployment's compiled source lets a
      session do. A deployment does not pick its four settings by hand; they follow from
      whether the application distribution ships a `SourceCompiler` and whether that
      compiler ran for this release, which is why an adapter returns them as one value
      through its source capability. Omitting it means no compiled source.
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
                 click_policy: ClickPolicy | None = None,
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
                 allow_page_facets: bool = False,
                 page_facet_policy: Callable[[PageObservation], bool] | None = None,
                 application_forms: ApplicationFormPort | None = None,
                 deterministic_read_navigation: bool = False,
                 skill_first: bool = False,
                 page_write_guard: bool = False,
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
        if not isinstance(allow_page_facets, bool):
            raise ValueError("allow_page_facets must be a boolean")
        if allow_page_facets and not page_write_guard:
            raise ValueError("page facets require an application page write guard")
        if allow_page_facets and not callable(page_facet_policy):
            raise ValueError("page facets require an application page policy")
        if allow_page_facets and "apply_facet" in disabled:
            raise ValueError("enabled page facets cannot disable apply_facet")
        if application_forms is not None and not isinstance(
                application_forms, ApplicationFormPort):
            raise TypeError("application_forms must implement ApplicationFormPort or be None")
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
        # History is a possible context source, not a standing authority or a compulsory
        # token cost. Include it only when the current wording refers backwards or the server
        # is holding a clarification that this message may answer. The current page remains
        # the runtime baseline; Skills are selected below; repository facts stay lazy behind
        # lookup_source so no source bytes enter context before the intent needs them.
        pending_conversation = (
            (self.conversation_context or {}).get("pending_clarification") or {})
        self._conversation_context_selected = bool(
            self.conversation_context is not None
            and (pending_conversation or _REFERENCE_FOLLOWUP.search(task.prompt)))
        if application_routes is not None and not isinstance(
                application_routes, ApplicationRoutePort):
            raise TypeError("application_routes must implement ApplicationRoutePort or be None")
        if knowledge_router is not None and not isinstance(
                knowledge_router, ApplicationKnowledgePort):
            raise TypeError("knowledge_router must implement ApplicationKnowledgePort or be None")
        source_routes_enabled = bool(
            runtime_source or source_affordance_mode == "auto" or source_contract)
        if application_routes is None and (
                source_routes_enabled or knowledge_router is not None or allow_application_entry
                or application_forms is not None):
            raise ValueError(
                "source navigation, application entry and forms require an application "
                "route port")
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
        self.allow_page_facets = allow_page_facets
        self.page_facet_policy = page_facet_policy
        self._application_form_tools = (
            ApplicationFormTools(
                application_forms, application_id=application_routes.application_id)
            if application_forms is not None else None)
        self.deterministic_read_navigation = deterministic_read_navigation
        current_navigation_only = _is_navigation_only(task.prompt)
        self._navigation_only = current_navigation_only
        self._required_page_controls = required_page_controls(task.prompt)
        self._required_page_effects = required_page_effects(task.prompt)
        self._seen_active_filters: dict[str, set[str]] = {}
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
        #: No Skill trigger matched this request's own words, so the candidates are the
        #: catalog's bounded fallback rather than a selection for this task. They stay
        #: loadable -- a missed paraphrase must not hide the typed reads -- but they are
        #: presented as unmatched, because a page request handed five unrelated Skills
        #: as "candidates" spent most of its step budget querying them (P8 §17.5).
        self._skills_unmatched = False
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
            self._skills_unmatched = bool(selection.fallback_used and not prior_skills
                                          and self._task_business_skills)
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
                presented_as_unmatched=self._skills_unmatched,
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
        # Skill first: a request whose wording matched exactly one read-only Skill gets that
        # Skill loaded before the first model call -- its playbook, routes and citable
        # statements -- instead of a step spent deciding to load it. Source stays available
        # for what the Skill does not cover; a write Skill is never loaded this way.
        self.skill_first = bool(skill_first)
        self._skill_first_pending: BusinessSkill | None = None
        self._skill_statement_refs: dict[str, tuple[str, ...]] = {}
        if (self.skill_first and not self._auto_loaded_business_skill_ids
                and not self._skills_unmatched and not selection.fallback_used
                and not selection.mutation_surface_widened):
            matched = [skill for skill in self._task_business_skills
                       if skill.skill_id in selection.trigger_matches]
            if len(matched) == 1 and matched[0].read_only:
                skill = matched[0]
                self._loaded_business_skill_ids.add(skill.skill_id)
                self._auto_loaded_business_skill_ids.add(skill.skill_id)
                self._skill_first_pending = skill
                self.ledger.append(
                    "business_skill_auto_loaded", skill_id=skill.skill_id,
                    skill_sha256=skill.artifact_sha256,
                    tool_names=list(skill.tool_names), provenance=list(skill.provenance),
                    routes=list(skill.routes), reason="skill_first_unique_match")
        self._business_routes: set[str] = set()
        self._latest_business_routes: set[str] = set()
        self._business_record_routes: set[str] = set()
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
                "describe_form", "prepare_form",
            }
            collisions = business_tools.tool_names & reserved
            if collisions:
                raise ValueError(
                    f"business tool names collide with Agent tools: {sorted(collisions)}")
        # A runner may project a public task boundary into a pre-dispatch action policy.  The
        # tool remains visible so an attempted violation produces an ordinary ReAct result:
        # no browser command is sent, a fresh observation is read, and the model can revise
        # its next decision.  Hidden write authorisation remains the ControlPlane's job.
        self.disabled_actions = disabled if allow_page_facets else disabled | {"apply_facet"}
        if click_policy is not None and (not isinstance(click_policy, ClickPolicy)
                                         or "click" not in disabled):
            raise ValueError("a click policy only narrows a disabled click")
        self.click_policy = click_policy
        self.policy_blocks = 0
        self.policy_block_limit = policy_block_limit
        self.no_progress_repeat_limit = no_progress_repeat_limit
        #: Deciding the same thing twice on the same page is not progress, and the step
        #: budget finds that out only after spending every remaining step.
        self._repeats = RepeatGuard(no_progress_repeat_limit)
        self._source_page_progress = SourcePageProgress()
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
        # Answer-format repair may not reopen browser actions after an observed action.
        self._finalizing = False
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
        self._writes = WriteWatch(control=control, ledger=ledger, run_id=self.run_id,
                                  write_guard_declared=bool(page_write_guard))
        #: Set once the agent has been told a write of its own did not execute.
        self._challenged_false_success = False
        self._browser_attempts = 0
        #: Source-contract scopes declared by the Adapter and observed on the entry page.
        #: Empty until `observe_entry()` has run, which is why planning happens after it.
        self.served_webapps: set[str] = set()
        #: The page state the model declared as completing the request, if it has. The
        #: server ends the turn when a fresh page shows it; see `execution.target`.
        self._target: TargetState | None = None
        self._target_declarations = 0
        #: Content digests of the typed reads this turn has already been handed, by the
        #: step that first returned them. A later read with the same content adds no fact.
        self._read_digests: dict[str, int] = {}
        self._repeated_reads = 0
        #: A fresh page has shown the declared target and the model has not yet said
        #: whether that completes the request. Only `finish` and `set_target` are offered.
        self._target_reached = False
        self._pending_navigation_url = ""
        self._tool_surface = ToolSurface(self)

    # -- phases ------------------------------------------------------------------------
    # -- the loop ----------------------------------------------------------------------
    def run(self) -> RunResult:
        try:
            return run_loop(self)
        finally:
            self._writes.end_control()

    # -- declared target state -----------------------------------------------------------
    def target_settlement_available(self) -> bool:
        """Whether this turn may declare a page state for the server to settle on.

        Not while the turn is finalising an answer, not for the capability question, and
        not for a request that matched a typed mutation: that request ends on the write's
        own receipt, and a page state declared beside it would let a navigation stand in
        for the change the person asked for.
        """
        return (not self._finalizing and not self._capability_question
                and not self._explicit_mutation_intent)

    def _tool_set_target(self, turn: "_Turn", call: dict, name: str, args: dict) -> "_Next":
        """Record what the model says completes the request; note at once if the page shows it."""
        if self._target_declarations >= MAX_TARGET_DECLARATIONS:
            self.ledger.append("target_state_refused", step=turn.step,
                               reason="declaration_limit", limit=MAX_TARGET_DECLARATIONS)
            return self._reply(turn, call,
                               f"TARGET REFUSED: a turn may declare its target at most "
                               f"{MAX_TARGET_DECLARATIONS} times. Act on the target you "
                               "have, or finish.")
        try:
            target = parse_target(args)
        except ValueError as exc:
            self.ledger.append("target_state_refused", step=turn.step,
                               reason="invalid", detail=str(exc)[:300])
            return self._reply(turn, call, f"TARGET REFUSED: {exc}.")
        self._target_declarations += 1
        self._target = target
        self._remember_active_filters(turn.obs)
        # A new declaration reopens the turn: the earlier checkpoint, and the receipt that
        # said the earlier state was reached, no longer describe what the model is after.
        self._target_reached = False
        if (self._answer_action_receipt is not None
                and self._answer_action_receipt["kind"] == "target_state"):
            self._answer_action_receipt = None
        unmet = unmet_conditions(target, turn.obs,
                                 required_selected=self._required_page_controls,
                                 required_effects=self._required_page_effects,
                                 seen_active_filters=self._seen_active_filters)
        self.ledger.append("target_state_declared", step=turn.step, target=target.to_json(),
                           declaration=self._target_declarations,
                           unmet=list(unmet), page_url=turn.obs.url,
                           page_epoch=turn.obs.epoch)
        if not unmet:
            self._note_target_reached(turn.obs, step=turn.step, acted=False)
            return self._reply(turn, call, self._render_target(turn.obs))
        return self._reply(turn, call,
                           "TARGET SET: " + describe_target(target)
                           + ". Act towards it now; the server reports the moment a fresh "
                             "page observation shows it."
                           + "\nStill unmet: " + "; ".join(unmet))

    def _note_target_reached(self, obs: PageObservation, *, step: int, acted: bool) -> bool:
        """Record that a fresh page shows the declared target, and open the checkpoint.

        Called with the fresh observation of a successful, write-free page action, or at
        declaration time when the page already shows the target. The server does not end
        the turn here: it can verify the page state but not that the state is the whole of
        the request, and the paid multistep gate showed a model declaring only the first
        half of one. Only a pure navigation turn restricts later tools to finish or a new
        target; a request that also asks a question may continue to read or observe. The
        person-facing page statement is composed from the observation, not model prose.
        """
        self._remember_active_filters(obs)
        if (self._target is None or unmet_conditions(
                self._target, obs,
                required_selected=self._required_page_controls,
                required_effects=self._required_page_effects,
                seen_active_filters=self._seen_active_filters)):
            return False
        self._target_reached = True
        self._answer_action_receipt = {
            "kind": "target_state", "url": obs.url,
            "selected": list(dict.fromkeys(
                (*self._target.selected, *self._required_page_controls))),
            **({"filters": list(self._target.filters)} if self._target.filters else {}),
            **({"groups": list(self._target.groups)} if self._target.groups else {}),
            **({"filters_absent": list(self._target.filters_absent)}
               if self._target.filters_absent else {}),
            "acted": bool(acted), "page_actions": self.page_actions}
        self.ledger.append("target_state_reached", step=step,
                           target=self._target.to_json(), url=obs.url,
                           page_epoch=obs.epoch, acted=bool(acted),
                           page_actions=self.page_actions, side_effect_possible=False)
        return True

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
