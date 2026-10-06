"""The model loop: budget, cancellation, and the one tool each step may dispatch.

The loop asks the model, bounds the turn by wall clock, steps and spend, and dispatches
exactly one tool per step -- this session's named tools first, then the browser path.
Turn state itself stays with the session; the loop only moves it forward (P7.5 D-01).
"""
from __future__ import annotations

from functools import partial
import json
import time
from typing import Callable

from ..answer import answer_protocol, page_answer
from ..answer.answer_view import unavailable_answer
from ..transport.browser import TransportError, TurnCancelled
from ..business.controlplane import ControlPlaneError
from ..modeling.egress import ProviderEgressError
from ..modeling.provider import ProviderError
from ..modeling.spend import BudgetExhausted
from ..privacy import safe_tool_arguments as _safe
from .business_tools import apply_skill_first, business_tool, load_skill, prefill_form
from .data_tools import data_tool
from ..goal.read_tools import DATA_READ_NAMES
from .form_tools import describe_form, prepare_form
from .form_entries import find_form_entries, open_form_entry
from .knowledge_tools import (ask_user_tool, check_capability_tool, capability_check_available,
                              lookup_source_history_tool, lookup_source_tool, read_source_tool)
from .page_lifecycle import acquire_page, requires_page
from .page_actions import take_page_action
from .prompts import (AGENT_SYSTEM, APPLICATION_ENTRY_SYSTEM, BUSINESS_SKILL_SYSTEM,
                      CAPABILITY_SYSTEM, CLARIFICATION_SYSTEM, CONVERSATION_SYSTEM,
                      FORM_SYSTEM, REACT_SOURCE_SYSTEM, SOURCE_ROUTE_SYSTEM, SCOPE_FIRST_SYSTEM, SKILL_SYSTEM,
                      SKILL_FIRST_SYSTEM, SOURCE_AFFORDANCE_SYSTEM, TARGET_SYSTEM,
                      UNMATCHED_SKILL_SYSTEM,
                      _assistant_call, _tool_result, _trim)
from .runstate import RunResult, _Next, _Turn


# -- one tool, one place ------------------------------------------------------------
def handle_named_tool(session, turn: "_Turn", call: dict, name: str,
                      args: dict) -> "_Next | None":
    """Run the tool this session offers under `name`, or answer `None` for someone else."""
    handler = named_tools(session).get(name)
    return None if handler is None else handler(turn, call, name, args)


def named_tools(session):
    """The tools this session is currently offering, by name.

    Availability is asked for here rather than repeated as a condition next to each
    branch, so a tool the session does not offer cannot be half-handled.
    """
    available: dict[str, Callable[["_Turn", dict, str, dict], "_Next"]] = {}
    if session.require_task_contract:
        available["declare_task"] = session._tool_declare_task
    if session.runtime_source:
        available["lookup_source"] = partial(lookup_source_tool, session)
        available["lookup_source_history"] = partial(lookup_source_history_tool, session)
        if session.source.reader_lookup() is not None:
            available["read_source"] = partial(read_source_tool, session)
    if session._task_business_skills:
        available["load_skill"] = partial(load_skill, session)
    if session.allow_form_prefill:
        available["prefill_form"] = partial(prefill_form, session)
        if session._application_form_tools is not None:
            available["describe_form"] = partial(describe_form, session)
            available["prepare_form"] = partial(prepare_form, session)
        if session._form_entry_tools is not None:
            available["find_form_entries"] = partial(find_form_entries, session)
            available["open_form_entry"] = partial(open_form_entry, session)
    if session._answers_pending_question:
        available["withdraw_request"] = session._tool_withdraw_request
    if (session.business_tools is not None
            or session.data_reads is not None
            or session.evidence.has_application_evidence()):
        available["compute"] = session._tool_compute
    if session.allow_clarification:
        available["ask_user"] = partial(ask_user_tool, session)
    if capability_check_available(session):
        available["check_capability"] = partial(check_capability_tool, session)
    if session.target_settlement_available():
        available["set_target"] = session._tool_set_target
    available["finish"] = session._tool_finish
    if session.data_reads is not None:
        for tool in DATA_READ_NAMES:
            available[tool] = partial(data_tool, session)
    for tool in (session.business_tools.tool_names if session.business_tools is not None else ()):
        available[tool] = partial(business_tool, session)
    return available


#: Provider failures one more attempt may recover: the transport failed, the provider was
#: rate limited, or it said it was unavailable. Refusals, bad requests, authentication,
#: an open circuit, a deadline and a cancellation end the turn as before.
RETRYABLE_PROVIDER_CODES = frozenset({"provider_transport", "provider_rate_limited",
                                      "provider_unavailable"})
#: At most this many retried model calls in one turn; a provider that keeps failing is the
#: shared circuit breaker's business, not a loop's.
MAX_PROVIDER_RETRIES_PER_TURN = 2
#: A retry starts only with this much of the turn's wall budget left.
PROVIDER_RETRY_MIN_REMAINING_S = 5.0


def _spent(session, t0: float) -> float:
    """The turn's own time: wall-clock since it started, less what a person took to answer."""
    if session._turn_budget is not None:
        return session._turn_budget.clock() - session._turn_budget.started
    return time.time() - t0 - session._approvals.waited_ms / 1000


def _complete_once_more_if_transient(session, messages: list, step: int, t0: float) -> dict:
    """One model call, repeated once when it failed in a way a repeat may recover.

    A model call changes nothing but the bill, so a repeat is safe. The failed attempt keeps
    its own spend row with usage unknown, and the retry is an audited `provider_retry`
    event, so nothing about it is hidden from the turn's record.
    """
    for attempt in (1, 2):
        elapsed = _spent(session, t0)
        try:
            return session.chat.complete_cancellable(
                messages, session.tool_schemas(), session.run_id,
                timeout_s=max(0.001, session.wall_budget_s - elapsed),
                cancelled=session._cancel_requested,
            )
        except ProviderError as exc:
            code = getattr(exc, "code", "provider_error")
            used = getattr(session, "_provider_retries", 0)
            if (attempt == 2 or not getattr(exc, "retryable", False)
                    or code not in RETRYABLE_PROVIDER_CODES
                    or used >= MAX_PROVIDER_RETRIES_PER_TURN
                    or session.wall_budget_s - _spent(session, t0)
                    < PROVIDER_RETRY_MIN_REMAINING_S
                    or session._cancel_requested()):
                raise
            session._provider_retries = used + 1
            dispatched = bool(getattr(exc, "dispatched", None))
            session.ledger.append(
                "provider_retry", step=step, provider_error_code=code,
                provider_request_id=getattr(exc, "request_id", "") or "",
                dispatched=dispatched,
                accounting_status="usage_unavailable" if dispatched else "not_dispatched")
            pause = 1.0 if code == "provider_rate_limited" else 0.3
            until = time.monotonic() + pause
            while time.monotonic() < until:
                if session._cancel_requested():
                    raise
                time.sleep(0.05)
    raise AssertionError("unreachable")


def run_loop(session) -> RunResult:
    t0 = time.time()
    session._approvals.start_turn(int((t0 + session.wall_budget_s - _spent(session, t0)) * 1000))
    principal = session._approvals.principal
    session._repeats.bind_scope(session.run_id + ":" + (
        principal.binding_digest if principal is not None else "anonymous"))
    result = RunResult(run_id=session.run_id, task_ref=session.task.task_ref, arm=session.arm,
                       terminal_reason="agent_finished",
                       contract_digest=session.contract.content_digest,
                       contract_source=session.contract_source,
                       citations=len(session.contract.cited_keys()))
    try:
        if not session.defer_page_context or session.entry_observation is not None:
            session._writes.begin_control()
    except ControlPlaneError as exc:
        return session._finish(result, t0, "control_plane_error", str(exc))
    if session._cancel_requested():
        return session._finish(
            result, t0, "user_cancelled", "the user stopped this task")
    if session.planning_error:
        return session._finish(result, t0, session._planning_terminal or "planning_failed",
                               session.planning_error)
    apply_skill_first(session)
    try:
        obs = session.entry_observation
        if obs is None and not session.defer_page_context:
            obs = session.browser.observe()
    except TransportError as e:
        reason = "user_cancelled" if session._cancel_requested() else "transport_error"
        return session._finish(result, t0, reason, str(e))
    if obs is not None:
        session.ledger.append("observation", epoch=obs.epoch, url=obs.url,
                             elements=len(obs.elements), digest=obs.content_digest)

    system = AGENT_SYSTEM
    if session.require_task_contract:
        from .request_contract import declaration_system
        system += "\n" + declaration_system(session)
    else:
        from .request_contract import form_execution_system
        form_mode = form_execution_system(session)
        if form_mode:
            system += "\n" + form_mode
    if session.target_settlement_available():
        system += "\n" + TARGET_SYSTEM
    if session.page_answer_evidence:
        system += "\n" + page_answer.SYSTEM
    if session.runtime_source:
        system += "\n" + REACT_SOURCE_SYSTEM
        if session.source_map is not None:
            system += "\n" + SOURCE_ROUTE_SYSTEM
    if session.source_affordance_mode in ("auto", "empty"):
        system += "\n" + SOURCE_AFFORDANCE_SYSTEM
    if session.knowledge_router is not None:
        system += "\n" + SKILL_SYSTEM
    if session.allow_application_entry:
        system += "\n" + APPLICATION_ENTRY_SYSTEM
    if session._task_business_skills:
        system += "\n" + BUSINESS_SKILL_SYSTEM
        if session._skills_unmatched:
            system += "\n" + UNMATCHED_SKILL_SYSTEM
        elif session.skill_first and session.runtime_source:
            system += "\n" + SKILL_FIRST_SYSTEM
    if session.allow_form_prefill and session._application_form_tools is not None:
        system += "\n" + FORM_SYSTEM
    if session._conversation_context_selected:
        system += "\n" + CONVERSATION_SYSTEM
    if session.allow_clarification:
        system += "\n" + CLARIFICATION_SYSTEM
    if session._capability_question:
        system += "\n" + CAPABILITY_SYSTEM
    if session.scope_first_policy:
        system += "\n" + SCOPE_FIRST_SYSTEM
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": session._opening(obs)}]
    session._sync_plan_message(messages)
    try:
        if session._writes.control_started:
            session._writes.start_observing()
    except ControlPlaneError as e:
        return session._finish(result, t0, "control_plane_error", str(e))
    step = 0
    successful_business_tools = 0
    #: The same state the loop keeps in locals, in the form a step handler takes.
    turn = _Turn(result=result, started_at=t0, messages=messages, obs=obs)
    #: A reply the model gave as plain content and was then asked to follow with a tool
    #: call. When the next call is `finish(done=true)`, this is what the user reads: the
    #: model's `why` at that point describes the reply ("told the user what I can do")
    #: rather than being one, and showing it replaced a complete answer with a run report.
    unanswered_reply = ""
    last_tool_name = ""
    while True:
        if obs is not None and session._page_answer_digest and session._page_answer_digest != obs.content_digest:
            session.evidence.discard_pages()
            session._page_answer_digest = session._page_answer_window_digest = ""
        if (obs is not None and session._answer_action_receipt is not None
                and session._answer_action_receipt["kind"] in
                    {"navigation", "page_interaction", "target_state"}
                and session._answer_action_receipt["url"] != obs.url):
            session._answer_action_receipt = None
        if (obs is not None and session._answer_action_receipt is not None
                and session._answer_action_receipt.get("navigation_url", obs.url) != obs.url):
            session._answer_action_receipt.pop("navigation_url", None)
        if session._cancel_requested():
            return session._finish(
                result, t0, "user_cancelled", "the user stopped this task",
                steps=step)
        if (last_tool_name in {"lookup_source", "lookup_source_history", "read_source", "observe",
                               "click", "goto", "back", "scroll"}
                and session.evidence.has_source_evidence()
                and not session.evidence.has_typed_evidence()
                and not session._explicit_mutation_intent):
            source_refs = tuple(ref for ref in session.evidence.ids()
                                if ref.startswith(("source#", "history#")))
            progress = session._source_page_progress.note(obs, source_refs)
            if progress == "warn":
                session.ledger.append("source_page_no_progress", step=step,
                                      action="warn", last_tool=last_tool_name)
                messages += [{"role": "user", "content":
                             "The recent source/page steps returned no new fact or "
                             "page state. Use the evidence already held, change the "
                             "investigation materially, or finish with a limited result."}]
                messages = _trim(messages, session.keep_observations)
            elif progress == "end":
                result.agent_said = (
                    "I could not establish a new fact or page state after repeated "
                    "source/page steps, so I stopped without claiming completion.")
                session.ledger.append("source_page_no_progress", step=step,
                                      action="end", last_tool=last_tool_name)
                return session._finish(result, t0, "no_progress", result.agent_said,
                                       steps=step)
        if step >= session.step_budget:
            return session._finish(result, t0, "step_budget", f"used all {session.step_budget} steps",
                                steps=step)
        elapsed = _spent(session, t0)
        if elapsed >= session.wall_budget_s:
            return session._finish(result, t0, "wall_clock_budget",
                                f"exceeded {session.wall_budget_s}s", steps=step)
        session._feed.note(stage="thinking", step=step + 1)
        answer_contract = ("\n" + answer_protocol.ANSWER_INSTRUCTIONS
            if session.evidence.ids() or (session._task_requirements is not None
                and session._task_requirements.answer_requests) else "")
        if session.require_task_contract:
            # Only declare_task is offered before interpretation. Source, page,
            # form and answer instructions belong to execution, and distracting
            # the declaration with them also pays for them unnecessarily. Restore
            # the ordinary complete execution contract as soon as it is accepted.
            from .request_contract import declaration_system
            content = (declaration_system(session)
                       if session._task_requirements is None else system + answer_contract)
            messages[0] = {"role": "system", "content": content}
        else:
            # One complete copy once facts are needed; never accumulate another
            # copy per read or burden an operation without an evidence answer.
            messages[0] = {"role": "system", "content": system + answer_contract}
        try:
            reply = _complete_once_more_if_transient(session, messages, step, t0)
        except TurnCancelled:
            return session._finish(result, t0, "user_cancelled",
                                   "the user stopped this task", steps=step)
        except BudgetExhausted as e:
            if session._cancel_requested():
                return session._finish(
                    result, t0, "user_cancelled",
                    "the user stopped this task while the provider was working",
                    steps=step)
            return session._finish(result, t0, "step_budget" if e.kind == "step_budget"
                                else "token_budget" if e.kind == "token_budget"
                                else "wall_clock_budget", str(e), steps=step)
        except (ProviderError, ProviderEgressError) as e:
            provider_code = getattr(e, "code", "provider_error")
            result.provider_error_code = provider_code
            request_id = getattr(e, "request_id", "")
            dispatched = getattr(e, "dispatched", None)
            if provider_code == "provider_cancelled":
                session.ledger.append(
                    "provider_cancelled",
                    step=step,
                    dispatched=bool(dispatched),
                    accounting_status=("usage_unavailable" if dispatched
                                       else "not_dispatched"),
                )
                return session._finish(
                    result, t0, "user_cancelled",
                    "the user stopped this task while the provider was working",
                    steps=step)
            if provider_code == "provider_deadline":
                session.ledger.append(
                    "provider_deadline",
                    step=step,
                    dispatched=bool(dispatched),
                    accounting_status=("usage_unavailable" if dispatched
                                       else "not_dispatched"),
                )
                return session._finish(
                    result, t0, "wall_clock_budget",
                    "the provider request reached the turn deadline",
                    steps=step)
            if request_id:
                session.ledger.append(
                    "provider_failed",
                    step=step,
                    provider_request_id=request_id,
                    provider_error_code=provider_code,
                    retryable=bool(getattr(e, "retryable", False)),
                )
            reason = ("wall_clock_budget"
                      if _spent(session, t0) >= session.wall_budget_s
                      else "provider_error")
            return session._finish(result, t0, reason, str(e), steps=step)

        if _spent(session, t0) >= session.wall_budget_s:
            return session._finish(
                result, t0, "wall_clock_budget",
                f"provider returned after the {session.wall_budget_s}s deadline",
                steps=step,
            )
        if session._cancel_requested():
            return session._finish(
                result, t0, "user_cancelled",
                "the user stopped this task while the provider was working",
                steps=step,
            )

        calls = reply.get("tool_calls") or []
        if not calls:
            said = str(reply.get("content") or "").strip()
            if session._pending_action is not None:
                step += 1
                messages += [{"role": "assistant", "content": said},
                             {"role": "user", "content":
                              "The acknowledged action is still unverified. Observe or wait_for "
                              "its result; do not repeat it or claim completion. Finish unable "
                              "if the result cannot be verified."}]
                continue
            if session.require_task_contract and session._task_requirements is None:
                step += 1
                session.ledger.append("task_declaration_required", step=step,
                                      tool="plain_content", dispatched=False)
                messages += [{"role": "assistant", "content": said},
                             {"role": "user", "content":
                              "First call declare_task for every USER REQUEST clause. "
                              "No application tool or completion has been dispatched."}]
                continue
            if session._answers.answer_mode or session.require_task_contract:
                step += 1
                session._answer_repairs += 1
                session.ledger.append("answer_contract_refused", step=step,
                                   reason="plain_content", repair=session._answer_repairs)
                if (session._refused_reads
                        and not session.evidence.has_application_evidence()):
                    # An authorization refusal and nothing read: there is no answer to
                    # repair, and what the person has to hear is which operation their
                    # account was refused. Repairing here would spend model calls to
                    # reach the same end.
                    result.agent_said = unavailable_answer(session.task.prompt)
                    return session._finish(result, t0, "permission_denied",
                                        session._refused_detail, steps=step)
                if session._answer_repairs > answer_protocol.MAX_ANSWER_REPAIRS:
                    if session._task_requirements is not None:
                        return session._refuse_unfinished_task(result, t0, step, obs)
                    result.agent_said = unavailable_answer(session.task.prompt)
                    return session._finish(result, t0, "agent_refused", result.agent_said,
                                        steps=step)
                messages += [{"role": "assistant", "content": said},
                             {"role": "user", "content":
                              "Submit finish with outcome and an evidence-backed answer. "
                              "Use compute if needed; do not repeat the page observation. "
                              "Use outcome=unable if you cannot support the answer."}]
                continue
            minimum_page_actions = \
                session._answers.plain_business_answer_min_page_actions
            if (successful_business_tools and said
                    and minimum_page_actions is not None
                    and session.page_actions >= minimum_page_actions):
                # A normal tool-calling assistant returns its answer as plain content after
                # receiving the final read-only ToolResult. Requiring a synthetic finish
                # call here adds cost without adding evidence. The product-level verifier
                # still decides whether this unverified answer satisfies the user task.
                step += 1
                session.ledger.append(
                    "business_answer", step=step, said=said[:300],
                    successful_business_tools=successful_business_tools,
                    success_claim="unverified")
                result.business_answer_projected = \
                    session._answers.business_answer_is_projectable(session.page_actions)
                result.agent_said = said[:session.max_agent_said_chars]
                return session._finish(
                    result, t0, "agent_finished", result.agent_said, steps=step)
            # No tool call is not a terminal event by itself: nudge once per step, the
            # step budget still bounds it. Read-only business answers are the exception
            # above because they are checked by the product verifier after this loop.
            step += 1
            unanswered_reply = said
            messages += [{"role": "assistant", "content": reply.get("content") or ""},
                         {"role": "user", "content":
                             "Call one of the tools. If your reply above already answers "
                             "the user, call finish with done=true and the user will read "
                             "that reply. If the task is impossible, call finish with "
                             "done=false."}]
            session.ledger.append("no_tool_call", step=step,
                               said=(reply.get("content") or "")[:300])
            continue
        if len(calls) > 1:
            # ReAct has exactly one action/receipt/observation transition.  Silently
            # taking calls[0] made an invalid reference in the discarded second call
            # look as though the provider or browser had failed.  Dispatch none: there
            # is no result for any proposed call, so the next inference starts from the
            # same page and receives explicit protocol feedback.
            step += 1
            names = [str((call.get("function") or {}).get("name") or "")[:100]
                     for call in calls]
            session.ledger.append("multiple_tool_calls_rejected", step=step,
                               count=len(calls), tools=names,
                               dispatched=False, side_effect_possible=False)
            messages += [
                {"role": "assistant", "content": reply.get("content") or ""},
                {"role": "user", "content":
                 "You proposed more than one tool call. None was dispatched. Call "
                 "exactly one tool, wait for its result, and then replan."},
            ]
            messages = _trim(messages, session.keep_observations)
            continue

        call = calls[0]
        name = (call.get("function") or {}).get("name") or ""
        last_tool_name = name
        raw_args = (call.get("function") or {}).get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        except json.JSONDecodeError:
            args = {}
        step += 1
        turn.messages, turn.obs, turn.step = messages, obs, step
        session.ledger.append("tool_call", step=step, tool=name,
                           args=_safe(args, session.session_handoffs))
        session._feed.note(
            stage="tool", tool=name, step=step,
            arguments=_safe(args, session.session_handoffs) if isinstance(args, dict) else {},
            mutation=bool(
                session.business_tools is not None
                and name in session.business_tools.tool_names
                and session.business_tools.is_mutation(name)))
        kept_reply, unanswered_reply = (unanswered_reply if name == "finish" else ""), ""
        turn.kept_reply = kept_reply

        if (session._pending_action is not None
                and name not in {"observe", "wait_for"}
                and not (name == "finish" and isinstance(args, dict)
                         and (args.get("outcome") in {"unable", "partial"}
                              or args.get("done") is False))):
            session.ledger.append("pending_action_tool_refused", step=step, tool=name,
                                  dispatched=False, side_effect_possible=False)
            messages += [_assistant_call(call), _tool_result(call,
                "ACTION RESULT PENDING: the acknowledged action was not repeated. Only observe, "
                "wait_for, or finish unable/partial may run until its result is verified.")]
            continue

        if session.require_task_contract and session._task_requirements is None and name != "declare_task":
            session.ledger.append("task_declaration_required", step=step, tool=name, dispatched=False)
            messages += [_assistant_call(call), _tool_result(call,
                "First declare_task for every original USER REQUEST clause. No application tool was dispatched.")]
            continue

        if obs is None and name not in {tool["function"]["name"] for tool in session.tool_schemas()}:
            session.ledger.append("unknown_tool_rejected", step=step, tool=name,
                                  dispatched=False, side_effect_possible=False)
            session._reply(turn, call, f"There is no available tool called {name!r}.")
            messages = turn.messages
            continue
        if obs is None and requires_page(session, name):
            prepared = acquire_page(session, turn, call, name)
            obs = turn.obs
            if prepared is not None:
                if prepared.finished is not None:
                    return prepared.finished
                messages = turn.messages
                continue
        elif obs is None and name not in named_tools(session):
            session.ledger.append("unknown_tool_rejected", step=step, tool=name,
                                  dispatched=False, side_effect_possible=False)
            session._reply(turn, call, f"There is no registered tool called {name!r}.")
            messages = turn.messages
            continue

        no_progress = session._repeats.decide(name, args, obs)
        if no_progress == "warn":
            session.ledger.append(
                "no_progress_detected", step=step, tool=name,
                repeated=session._repeats.repeats, dispatched=False,
                side_effect_possible=False)
            messages += [_assistant_call(call), _tool_result(
                call, "NO PROGRESS: the same tool and arguments were already used on "
                      "the same page state. Nothing was dispatched. Change strategy, "
                      "ask one necessary clarification, or finish honestly.")]
            continue
        if no_progress == "end":
            result.agent_said = (
                "I couldn't make progress after the same step was repeated. "
                "No repeated action was dispatched.")
            session.ledger.append(
                "no_progress_terminated", step=step, tool=name,
                repeated=session._repeats.repeats, dispatched=False,
                side_effect_possible=False)
            return session._finish(
                result, t0, "no_progress", result.agent_said, steps=step)

        # Decisions can arrive during model inference. Refresh before another browser
        # command or a completion claim; a trace window is not command correlation.
        if session._browser_attempts:
            try:
                session._writes.refresh_control()
            except ControlPlaneError as e:
                return session._finish(result, t0, "control_plane_error", str(e), steps=step)
            if session._writes.uncertain_write:
                return session._finish(result, t0, "indeterminate_write",
                                    session._writes.uncertain_write, steps=step)

        from .tools import answer_repair_tools
        if session._finalizing and name not in answer_repair_tools(session):
            session._answer_repairs += 1
            session.ledger.append("finalizing_tool_refused", step=step, tool=name,
                                  dispatched=False, repair=session._answer_repairs)
            if session._answer_repairs > answer_protocol.MAX_ANSWER_REPAIRS:
                result.agent_said = unavailable_answer(session.task.prompt)
                return session._finish(result, t0, "agent_refused", result.agent_said,
                                       steps=step)
            messages += [_assistant_call(call), _tool_result(
                call, "This turn is repairing the answer for an observed action. Only "
                      "observe, registered business reads, compute and finish may run. "
                      "Do not repeat the action or start another write. Submit evidence "
                      "for the missing answer, or report partial/unable.")]
            continue

        if (session._target_reached and session._navigation_only and not session._finalizing
                and name not in {"finish", "set_target", "declare_task", "ask_user", "withdraw_request"}):
            # A pure navigation request can stop at its page target. A request
            # that also asks a question still needs reads or observation after
            # reaching a page, so its target cannot lock the tool surface.
            session.ledger.append("target_checkpoint_tool_refused", step=step, tool=name,
                                  dispatched=False, side_effect_possible=False)
            messages += [_assistant_call(call), _tool_result(
                call, "TARGET REACHED: the page already shows the declared state. Only "
                      "finish, set_target and any offered declare_task are available. Call finish to report the "
                      "action if the request is complete, or set_target with the next "
                      "state if it is not.")]
            continue

        handled = handle_named_tool(session, turn, call, name, args)
        if handled is not None:
            if handled.finished is not None:
                return handled.finished
            messages, obs, step = turn.messages, turn.obs, turn.step
            successful_business_tools = turn.successful_business_tools
            continue

        outcome = take_page_action(session, turn, call, name, args)
        if outcome.finished is not None:
            return outcome.finished
        messages, obs, step = turn.messages, turn.obs, turn.step
        successful_business_tools = turn.successful_business_tools
