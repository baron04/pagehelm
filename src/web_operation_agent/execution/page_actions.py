"""The browser path of a step: refusal, routing, dispatch, receipt, write settlement.

A page action is the path the browser is on, and the routing facts it establishes -- an
exact source route, a skill's application entry, whether the route is read-only -- are
its own. The tools that answer for themselves live with what they mean (P7.5 D-01).
"""
from __future__ import annotations

import hashlib
import re
from urllib.parse import urljoin, urlsplit

from ..answer.evidence import EvidenceUnknown
from ..transport.browser import TransportError, TurnCancelled
from .grounding import WRITE_NONE, WRITE_PERMITTED
from ..privacy import safe_tool_arguments as _safe
from ..schemas import (BROWSER_ACTIONS, POTENTIALLY_WRITING_ACTIONS, ActionReceipt,
                       PageObservation)
from .click_policy import click_policy_refusal, reviewed_destination
from .knowledge_tools import capability_check_available, capability_check_feedback
from .page_evidence import capture_page_snapshot
from .prompts import _assistant_call, _tool_result, _trim
from .runstate import _Next, _StepEnd, _Turn


def take_page_action(session, turn: "_Turn", call: dict, name: str, args: dict) -> "_Next":
    """Everything a page action is: refuse it, route it, dispatch it, report what it did.

    The tools above answer for themselves; this is the path the browser is on, and the
    routing facts it establishes -- an exact source route, a skill's application entry,
    whether the route is read-only -- are its own, which is why they were the loop's
    last locals.
    """
    result, t0, step = turn.result, turn.started_at, turn.step
    messages, obs = turn.messages, turn.obs
    successful_business_tools = turn.successful_business_tools
    if obs.render_state == "pending" and name not in {"observe", "wait_for"}:
        session.ledger.append("pending_page_action_refused", step=step, tool=name,
                              dispatched=False, side_effect_possible=False)
        messages += [_assistant_call(call), _tool_result(
            call, "PAGE RENDERING PENDING: the destination view is not yet "
                  "distinguishable from the preceding observation. The action was not "
                  "dispatched. Observe the page again "
                  "before using its controls or claiming arrival.")]
        return session._resume(turn, _trim(messages, session.keep_observations), obs,
                               successful_business_tools)
    click_refusal = ""
    if name == "click" and name in session.disabled_actions \
            and session.click_policy is not None:
        click_refusal = click_policy_refusal(session.click_policy, obs, args.get("ref"))
        element = next((e for e in obs.elements if e.ref == args.get("ref")), None)
        session.ledger.append(
            "click_policy_decision", step=step, allowed=not click_refusal,
            reason=click_refusal[:200], page_url=obs.url,
            role=element.role if element is not None else "",
            element_name=(element.name[:80] if element is not None else ""))
    if name in session.disabled_actions and not (
            name == "click" and session.click_policy is not None and not click_refusal):
        # This refusal is known before dispatch, so it is safe to continue.  Do not
        # manufacture a failed ActionReceipt for the requested action: that would blur
        # the decisive fact that the browser never received it.  The real receipt is
        # for the fresh observe that follows the refusal.
        session.policy_blocks += 1
        prior_epoch = obs.epoch
        session.ledger.append("action_policy_blocked", step=step, tool=name,
                           args=_safe(args, session.session_handoffs), dispatched=False,
                           side_effect_possible=False,
                           page_epoch_before=prior_epoch)
        try:
            session._browser_attempts += 1
            receipt = session.browser.execute("observe", {})
        except TransportError as e:
            return _Next(session._finish(result, t0,
                                session._transport_terminal("observe", may_write=False),
                                str(e), steps=step))
        write = session._writes.write_outcome(receipt, step, action_may_write=False)
        observed_epoch = (receipt.observation.epoch
                          if receipt.observation is not None else None)
        fresh_observation = bool(
            receipt.action == "observe" and receipt.ok
            and receipt.observation is not None
            and observed_epoch > prior_epoch)
        session.ledger.append("action_receipt", step=step, tool="observe",
                           command_id=receipt.command_id, after_tool=name,
                           policy_block=True, requested_tool_dispatched=False,
                           write_attribution="unattributed", ok=receipt.ok,
                           error_code=receipt.error_code,
                           navigated=receipt.navigated, url_after=receipt.url_after,
                           elapsed_ms=receipt.elapsed_ms, write=write["state"],
                           relogin=receipt.relogin,
                           page_epoch_before=prior_epoch,
                           observation_epoch=observed_epoch,
                           fresh_observation=fresh_observation)
        reason, detail = session._writes.settle_write(write)
        if reason:
            return _Next(session._finish(result, t0, reason, detail, steps=step))
        if not fresh_observation:
            return _Next(session._finish(
                result, t0, "transport_error",
                "action-policy refusal could not obtain a successful fresh observation",
                steps=step))
        if receipt.observation is not None:
            obs = receipt.observation
        if (session.policy_block_limit is not None
                and session.policy_blocks >= session.policy_block_limit):
            detail = ("stopped after "
                      f"{session.policy_blocks} pre-dispatch action-policy refusals")
            session.ledger.append("action_policy_budget_exhausted", step=step,
                               blocks=session.policy_blocks,
                               limit=session.policy_block_limit)
            return _Next(session._finish(result, t0, "action_policy_budget", detail,
                                steps=step))
        session._sync_plan_message(messages)
        feedback = (
            f"The run's action policy blocked {name!r} before dispatch"
            + (f" ({click_refusal})" if click_refusal else "") + ". "
            "The browser did not receive that requested action and it could not "
            "change the application. Inspect the fresh page and choose another "
            "available action.\n\n" + session._render(obs, receipt, "", write))
        messages += [_assistant_call(call), _tool_result(call, feedback)]
        messages = _trim(messages, session.keep_observations)
        return session._resume(turn, messages, obs, successful_business_tools)

    if _leaves_the_page(name, args, obs) and session.evidence.has_source_evidence() \
            and session._target is None:
        # Deployment source answers what a release declares; another page cannot. Every
        # paid Odoo run that drifted had the answering fact at its first lookup and then
        # went browsing to confirm it until the budget ran out. Leaving the page is still
        # possible -- as a declared page goal, which the target checkpoint then settles.
        session.ledger.append("source_navigation_requires_target", step=step, tool=name,
                              dispatched=False, side_effect_possible=False)
        messages += [_assistant_call(call), _tool_result(
            call, "NAVIGATION NOT DISPATCHED: this turn holds deployment-source evidence "
                  "and no declared target. Another page cannot show what the release "
                  "declares. If the user asked you to open or change a page, call "
                  "set_target with that page state first. Otherwise answer from the "
                  "evidence you hold, stating what it does not establish with "
                  "coverage=partial, or look up source with different words.")]
        messages = _trim(messages, session.keep_observations)
        return session._resume(turn, messages, obs, successful_business_tools)

    source_route_citations: list[str] = []
    source_route_read_only = False
    latest_business_route = False
    skill_app_entry = None
    # The routing tools resolve to a browser action rather than being one. Each of
    # them used to carry its own copy of the refusal sequence -- ledger, observe,
    # settle, render, continue -- and the copies had already begun to differ.
    routing = session._router.tools(
        skill_ids=session._task_skill_ids,
        affordances=session._source_affordances).get(name)
    if routing is not None:
        resolution = routing.resolve(args, obs, step)
        if resolution.refused:
            ended = _refuse_before_dispatch(session, 
                name, routing.explanation(resolution.rejection),
                obs=obs, step=step, no_fresh_detail=routing.no_fresh_detail)
            if ended.terminal:
                return _Next(session._finish(result, t0, ended.reason, ended.detail,
                                    steps=step))
            obs = ended.observation
            if routing.resync_plan_on_refusal:
                session._sync_plan_message(messages)
            messages += [_assistant_call(call), _tool_result(call, ended.feedback)]
            messages = _trim(messages, session.keep_observations)
            return session._resume(turn, messages, obs, successful_business_tools)
        source_route_read_only = True
        source_route_citations = list(resolution.citations)
        skill_app_entry = resolution.skill
        name, args = resolution.name, dict(resolution.args)

    if name == "open_source_route" and session.runtime_source:
        url = args.get("url")
        parsed = urlsplit(url) if isinstance(url, str) else None
        safe_relative = bool(parsed and url.startswith("/") and not url.startswith("//")
                             and not parsed.scheme and not parsed.netloc
                             and "%" not in parsed.path
                             and ".." not in parsed.path.split("/"))
        identity = (session._contract_route_identity(url)
                    if safe_relative else ("", ""))
        citations = session._source_view_routes.get(identity)
        if not citations:
            messages += [_assistant_call(call), _tool_result(
                call, "That route was not established as a cited read-only request by "
                      "a prior source lookup. Query source, use a returned exact route, "
                      "or act on the current page.")]
            session.ledger.append("source_route_rejected", step=step,
                               url=str(url or "")[:500])
            return session._resume(turn, messages, obs, successful_business_tools)
        source_route_citations = sorted(citations)
        session.ledger.append(
            "source_route_opened", step=step, url=str(url)[:500],
            citations=source_route_citations, route_intent="read_only",
            side_effect_possible=True)
        source_route_read_only = True
        name, args = "goto", {"url": url}

    if name == "check_capability" and capability_check_available(session):
        messages += [_assistant_call(call),
                     _tool_result(call, capability_check_feedback(session))]
        return session._resume(turn, messages, obs, successful_business_tools)

    if name not in BROWSER_ACTIONS:
        session.ledger.append(
            "unknown_tool_rejected", step=step, tool=str(name)[:100],
            dispatched=False, side_effect_possible=False)
        messages += [_assistant_call(call),
                     _tool_result(call, f"There is no tool called {name!r}. "
                                        f"Available: {sorted(BROWSER_ACTIONS)} and finish.")]
        return session._resume(turn, messages, obs, successful_business_tools)

    collect_page_answer = False
    if name == "observe" and session.page_answer_evidence:
        if (set(args) - {"answer_evidence"}
                or not isinstance(args.get("answer_evidence", False), bool)):
            messages += [_assistant_call(call), _tool_result(
                call, "observe accepts only an optional boolean answer_evidence.")]
            return session._resume(turn, messages, obs, successful_business_tools)
        collect_page_answer = args.get("answer_evidence", False)
        if collect_page_answer and session._refused_reads:
            messages += [_assistant_call(call), _tool_result(
                call, "PAGE EVIDENCE REFUSED: an authorization refusal in this turn "
                "closed the page as a source of facts. Answer from the typed reads "
                "you already have, or finish with outcome=unable.")]
            return session._resume(turn, messages, obs, successful_business_tools)
        if collect_page_answer:
            session._answer_mode = True
            session.evidence.discard_pages()
            session._page_answer_digest = ""
        args = {}

    if name == "goto" and not source_route_read_only:
        url = args.get("url")
        if isinstance(url, str) and url in session._business_routes:
            source_route_read_only = True
            latest_business_route = url in session._latest_business_routes
            session.ledger.append(
                "business_route_opened", step=step,
                url_digest=hashlib.sha256(url.encode()).hexdigest(),
                route_intent="read_only", side_effect_possible=True)
        elif isinstance(url, str) and _reviewed_address(session, url, obs):
            # The application reviewed this page as read-only; opening it by address is
            # what its menu link would do, and the switch below is still confirmed. The
            # model otherwise had to find the menu from wherever it stood, and from a page
            # with the menu closed it could only repeat the refused address.
            session.ledger.append(
                "reviewed_destination_opened", step=step,
                url_digest=hashlib.sha256(url.encode()).hexdigest(),
                side_effect_possible=True)
        elif not isinstance(url, str) or not _page_link_allows(url, obs):
            messages += [_assistant_call(call), _tool_result(
                call, "Direct navigation is allowed only to an exact link on the "
                      "current page, an exact ui_path from a successful business "
                      "ToolResult, or a cited source route.")]
            session.ledger.append("page_route_rejected", step=step,
                               url=str(url or "")[:500], page_url=obs.url)
            return session._resume(turn, messages, obs, successful_business_tools)

    # A matched typed mutation is the authoritative path for this request. Letting the
    # model open a related record before it calls that tool produces a navigation card in
    # place of the mutation's own pre-dispatch decision (J3.09 did this for an order with
    # nothing to invoice). A completed mutation may still open the exact route its result
    # returned; an integration with no matching mutation capability is unaffected.
    if (name == "goto" and source_route_read_only
            and session._explicit_mutation_intent
            and session._approvals.business_counts["mutation_completed"] == 0):
        session.ledger.append(
            "explicit_mutation_navigation_rejected", step=step,
            url_digest=hashlib.sha256(str(args.get("url") or "").encode()).hexdigest(),
            dispatched=False, side_effect_possible=False)
        messages += [_assistant_call(call), _tool_result(
            call, "This request matched an installed typed mutation. Do not navigate to "
                  "the related record as a substitute. Call that mutation tool so its "
                  "authoritative pre-dispatch check can either propose the exact change "
                  "or refuse it without asking the user to confirm.")]
        messages = _trim(messages, session.keep_observations)
        return session._resume(turn, messages, obs, successful_business_tools)

    # A route can be read-only in business intent and still cause an application request
    # with side effects. Route provenance constrains where navigation may go; it never
    # downgrades transport write risk or bypasses the control witness.
    action_may_write = name in POTENTIALLY_WRITING_ACTIONS
    if session.pending_write and action_may_write:
        return _Next(session._finish(result, t0, "indeterminate_write",
                            "another potentially writing action was requested while a "
                            "previous authorised request remains unconfirmed", steps=step))

    expect_nav = bool(args.pop("expect_navigation", False))
    if "ref" in args:
        args["epoch"] = obs.epoch
    if session._cancel_requested():
        return _Next(session._finish(
            result, t0, "user_cancelled",
            "the user stopped this task before browser dispatch", steps=step))
    link = _clicked_link(args, obs) if name == "click" else None
    navigating = name in ("goto", "back") or (
        name == "click" and (expect_nav or link is not None))
    if navigating:
        reason, detail = session._approvals.confirm_navigation(
            step, obs, action=name,
            url=(str(args.get("url") or "") if name == "goto" else (link or "")))
        if reason:
            return _Next(session._finish(result, t0, reason, detail, steps=step))
    prior_page_epoch = obs.epoch
    try:
        session._browser_attempts += 1
        session._writes.expect_decision(action_may_write)
        # A previous completed action remains true after a failed or read-only
        # follow-up. Replace its receipt only when this action establishes a new
        # navigation or an observed page interaction below. Route drift is checked
        # by the main loop before the next model decision.
        receipt = session.browser.execute(name, args, expect_navigation=expect_nav)
        if _answer_closed_the_menu(session, name, link, receipt, obs):
            # Answering the confirmation is a click on the page, and the application closed
            # the menu this link was in. The click never dispatched, the person confirmed
            # this exact destination, and the link was on the page the model acted on: go
            # there by its address rather than make the model reopen the menu, which spent
            # two steps per menu and ran a two-menu task out of budget.
            session.ledger.append(
                "confirmed_switch_opened_by_address", step=step,
                url_digest=hashlib.sha256(link.encode()).hexdigest(),
                reason="confirmation_closed_the_menu", side_effect_possible=True)
            session._browser_attempts += 1
            receipt = session.browser.execute("goto", {"url": link}, expect_navigation=True)
    except TurnCancelled as exc:
        session.ledger.append(
            "browser_action_cancelled_before_dispatch", step=step, tool=name,
            dispatched=False, side_effect_possible=False)
        return _Next(session._finish(
            result, t0, "user_cancelled", str(exc), steps=step))
    except TransportError as e:
        return _Next(session._finish(result, t0,
                            session._transport_terminal(name, may_write=action_may_write), str(e),
                            steps=step))

    if name not in ("observe", "wait_for"):
        session._approvals.settle_navigation(receipt, navigating=navigating)
    if name != "observe" and receipt.error_code != "stale_observation":
        session.page_actions += 1
    write = session._writes.write_outcome(
        receipt, step, action_may_write=action_may_write)
    session.ledger.append("action_receipt", step=step, tool=receipt.action,
                       command_id=receipt.command_id, write_attribution="unattributed",
                       ok=receipt.ok, error_code=receipt.error_code,
                       navigated=receipt.navigated, url_after=receipt.url_after,
                       elapsed_ms=receipt.elapsed_ms, write=write["state"],
                       # Whether the browser had to log in again to perform this
                       # action. It is on every receipt and was written to no log, so
                       # a run that kept arriving at a login page could not be told
                       # apart from one that was quietly being recovered.
                       relogin=receipt.relogin,
                       # The application held this session read-only for the action.
                       write_guard_held=receipt.write_guard_held,
                       source_route_citations=source_route_citations)
    if receipt.error_code == "stale_observation":
        # A document replacement can preserve the same DOM projection while
        # invalidating the observation identity. The rejected command was never
        # dispatched, so let the next ReAct decision retry against the fresh fact.
        session._repeats.reset()
    reason, detail = session._writes.settle_write(write)
    if reason:
        return _Next(session._finish(result, t0, reason, detail, steps=step))
    if receipt.observation is not None:
        obs = receipt.observation
    if (session._pending_navigation_url and obs.url != session._pending_navigation_url):
        session._pending_navigation_url = ""
    if (session._pending_navigation_url and obs.render_state != "pending"
            and receipt.ok and receipt.observation is not None
            and receipt.observation.epoch > prior_page_epoch
            and write["state"] == WRITE_NONE):
        session._answer_action_receipt = {
            "kind": "navigation", "url": session._pending_navigation_url}
        session._pending_navigation_url = ""
    if ((name in ("goto", "back") or (name == "click" and receipt.navigated))
            and receipt.ok and receipt.observation is not None
            and receipt.observation.epoch > prior_page_epoch
            and obs.render_state == "pending"):
        session._pending_navigation_url = obs.url
    elif ((name in ("goto", "back") or (name == "click" and receipt.navigated))
            and receipt.ok and receipt.observation is not None
            and receipt.observation.epoch > prior_page_epoch):
        if session._answer_action_receipt is not None and session._answer_action_receipt["kind"] == "mutation":
            session._answer_action_receipt["navigation_url"] = obs.url
        else:
            session._answer_action_receipt = {"kind": "navigation", "url": obs.url}
    elif (name == "click" and receipt.ok and not receipt.navigated
          and receipt.observation is not None
          and receipt.observation.epoch > prior_page_epoch
          and write["state"] == "none"
          and obs.url == receipt.url_before
          and (obs.content != turn.obs.content or obs.title != turn.obs.title)):
        # A successful DOM-changing click is reportable as an interaction, not as a
        # verified business result. The answer may state only that the page changed;
        # application-specific completion still needs its own postcondition check.
        session._answer_action_receipt = {"kind": "page_interaction", "action": "click",
                                          "url": obs.url}
    if (receipt.ok and receipt.observation is not None
            and receipt.observation.epoch > prior_page_epoch
            and write["state"] == WRITE_NONE and not session.pending_write):
        # The declared target is checked against every fresh, write-free page, whatever
        # action produced it, after the action's own receipt is recorded so the target
        # receipt is the one a finish reports. A write in the same window is settled by
        # the write path above and the ordinary loop; a page state cannot vouch for a write.
        session._note_target_reached(obs, step=step, acted=True)
    target_observed = False
    if name == "goto" and isinstance(args.get("url"), str):
        expected = urlsplit(urljoin(receipt.url_before, args["url"]))
        actual = urlsplit(obs.url)
        target_observed = (
            expected.scheme == actual.scheme
            and expected.netloc == actual.netloc
            and (expected.path.rstrip("/") or "/")
            == (actual.path.rstrip("/") or "/")
            and expected.query == actual.query)
    if (session.deterministic_read_navigation and session._navigation_only
            and sum(skill.read_only for skill in session._task_business_skills) == 1
            and name == "goto" and latest_business_route
            and receipt.ok and receipt.observation is not None
            and receipt.observation.epoch > prior_page_epoch
            and target_observed and not session._page_goal_unmet(obs)):
        result.agent_said = (
            "已打开请求的页面。"
            if re.search(r"[\u3400-\u9fff]", session.task.prompt)
            else "Opened the requested page.")
        session.ledger.append(
            "deterministic_navigation_completed", step=step,
            url_digest=hashlib.sha256(obs.url.encode()).hexdigest(),
            observation_epoch=obs.epoch, side_effect_possible=False)
        return _Next(session._finish(
            result, t0, "agent_finished", result.agent_said, steps=step))
    if skill_app_entry is not None:
        gap = session.knowledge_router.gap_evidence(skill_app_entry, obs)
        session.ledger.append(
            "skill_app_observed", step=step, skill_id=skill_app_entry.skill_id,
            skill_sha256=skill_app_entry.artifact_sha256, **gap)
    if write["state"] == WRITE_PERMITTED and session.contract_source != "none" \
            and not session.plan_state.withdrawn:
        # A write authorisation pauses the plan to prevent a second submission. A
        # consumed permit is not a saved change or evidence that the task is complete.
        session.plan_state.pause_for_confirmation(
            "write permission was consumed; the application result is unconfirmed")
        session.ledger.append("plan_awaiting_confirmation", step=step)
    note = session._ground(obs, receipt, write["state"], write["refused_requests"])
    session._sync_plan_message(messages)
    page_feedback = ""
    rendered_observation = obs
    if collect_page_answer:
        session._answer_mode = True
        if (receipt.ok and receipt.action == "observe" and receipt.observation is not None
                and receipt.observation.epoch > prior_page_epoch):
            try:
                rendered_observation, page_feedback = capture_page_snapshot(
                    session, obs, step=step, reused_observation=False)
            except (ValueError, TypeError, EvidenceUnknown):
                page_feedback = "PAGE EVIDENCE UNAVAILABLE: the observation could not support an answer."
        else:
            page_feedback = "PAGE EVIDENCE UNAVAILABLE: observe did not return a successful fresh page."
    feedback = session._render(rendered_observation, receipt, note, write)
    if page_feedback:
        feedback += "\n" + page_feedback
    messages += [_assistant_call(call), _tool_result(call, feedback)]
    messages = _trim(messages, session.keep_observations)
    return session._resume(turn, messages, obs, successful_business_tools)


def _refuse_before_dispatch(session, tool: str, explanation: str, *,
                            obs: PageObservation, step: int,
                            no_fresh_detail: str) -> "_StepEnd":
    """Refuse a server-side tool, then hand the agent a page it can actually act on.

    A refusal that only says "no" leaves the model reasoning about a page it can no
    longer see, so every one of these paths observes first and renders the result. Four
    call sites wrote the sequence out and differed only in the tool's name and one
    sentence; a fifth copy is how one of them would come to stop settling its write.

    The caller keeps the ledger line that says *why* it refused -- that reason is
    specific to the tool -- and decides whether to re-sync the plan message.
    """
    try:
        receipt, write, fresh = _observe_after_source_rejection(session, obs, step, tool)
    except TransportError as exc:
        return _StepEnd(reason=session._transport_terminal("observe", may_write=False),
                        detail=str(exc))
    reason, detail = session._writes.settle_write(write)
    if reason:
        return _StepEnd(reason=reason, detail=detail)
    if not fresh:
        return _StepEnd(reason="transport_error", detail=no_fresh_detail)
    fresh_obs = receipt.observation
    note = session._ground(fresh_obs, receipt, write["state"], write["refused_requests"])
    return _StepEnd(observation=fresh_obs,
                    feedback=explanation + "\n\n"
                             + session._render(fresh_obs, receipt, note, write))

def _observe_after_source_rejection(session, obs: PageObservation, step: int,
                                    after_tool: str) -> tuple[ActionReceipt, dict, bool]:
    """Ground a server-side refusal in a real fresh page without dispatching it."""
    if session._cancel_requested():
        raise TurnCancelled("the user stopped this task before page observation")
    prior_epoch = obs.epoch
    session._browser_attempts += 1
    receipt = session.browser.execute("observe", {})
    write = session._writes.write_outcome(receipt, step, action_may_write=False)
    observed_epoch = receipt.observation.epoch if receipt.observation is not None else None
    fresh = bool(receipt.action == "observe" and receipt.ok
                 and receipt.observation is not None and observed_epoch > prior_epoch)
    session.ledger.append("action_receipt", step=step, tool="observe",
                       command_id=receipt.command_id, after_tool=after_tool,
                       source_candidate_rejection=(after_tool == "open_source_candidate"),
                       server_tool_rejection=after_tool,
                       requested_tool_dispatched=False, side_effect_possible=False,
                       write_attribution="unattributed", ok=receipt.ok,
                       error_code=receipt.error_code, navigated=receipt.navigated,
                       url_after=receipt.url_after, elapsed_ms=receipt.elapsed_ms,
                       write=write["state"], relogin=receipt.relogin,
                       page_epoch_before=prior_epoch, observation_epoch=observed_epoch,
                       fresh_observation=fresh)
    return receipt, write, fresh


#: Tools that resolve to opening another page, whatever route they are given.
_ROUTING_TOOLS = frozenset({"goto", "back", "open_source_route", "open_source_candidate",
                            "enter_application", "open_skill_route", "enter_skill_app"})


def _leaves_the_page(name: str, args: dict, obs: PageObservation) -> bool:
    """Whether this action opens another page rather than acting on the current one.

    A click counts only when it names a link to another path or query; a tab, a button or
    a same-document `#` anchor acts in place. A click's script may still navigate, which
    the receipt reports; this is a pre-dispatch intent check, not a guarantee.
    """
    if name in _ROUTING_TOOLS:
        return True
    if name != "click":
        return False
    href = _clicked_link(args, obs)
    if not href or href.lower().startswith("javascript:"):
        return False
    try:
        current = urlsplit(obs.url)
        target = urlsplit(urljoin(obs.url, href))
    except ValueError:
        return False
    return (target.scheme, target.netloc, target.path, target.query) != (
        current.scheme, current.netloc, current.path, current.query)


def _answer_closed_the_menu(session, name: str, link: str | None, receipt,
                            obs: PageObservation) -> bool:
    """A confirmed link click refused before dispatch, on the page it was aimed at.

    Only a refusal that proves nothing was dispatched, only a link with an address, only
    the destination the person confirmed, and only while the page is still the one the
    model acted on -- a page that moved on is the model's to re-plan.
    """
    return (name == "click" and bool(link)
            and receipt.error_code == "stale_observation"
            and receipt.url_after == obs.url
            and session._approvals.holds_confirmation(action="click", url=link))


def _clicked_link(args: dict, obs: PageObservation) -> str | None:
    """The target of the link a click names, "" for a link without one, None for no link.

    A link is what leaves the page, whatever role the application gave it: an ARIA
    `menuitem` or `tab` rendered as `<a href="/next">` navigates exactly like a `link`, and
    checking the role alone let an application-switcher entry change the page without the
    confirmation every other page switch asks for. A same-document `#` anchor is not a
    link in this sense; it stays where it is.
    """
    ref = args.get("ref")
    for element in obs.elements:
        if element.ref != ref:
            continue
        href = element.attrs.get("href") if isinstance(element.attrs, dict) else None
        if element.role == "link":
            return href if isinstance(href, str) else ""
        if isinstance(href, str) and href.strip() and not href.strip().startswith("#"):
            return href
        return None
    return None


def _reviewed_address(session, url: str, obs: PageObservation) -> bool:
    """A same-origin address the application's click policy reviewed as a destination."""
    if session.click_policy is None:
        return False
    try:
        current = urlsplit(obs.url)
        wanted = urlsplit(urljoin(obs.url, url))
    except (TypeError, ValueError):
        return False
    if (wanted.scheme, wanted.netloc) != (current.scheme, current.netloc) or wanted.fragment:
        return False
    relative = wanted.path + (f"?{wanted.query}" if wanted.query else "")
    return reviewed_destination(session.click_policy, relative)


def _page_link_allows(url: str, obs: PageObservation) -> bool:
    """Whether ``url`` is the exact same-origin target of a current-page link.

    The model prompt has always limited generic ``goto`` this way, but a prompt is not a
    boundary.  Source-only navigation has its own cited view-route validator and bypasses
    this function explicitly in the session loop.
    """
    try:
        current = urlsplit(obs.url)
        wanted = urlsplit(urljoin(obs.url, url))
    except (TypeError, ValueError):
        return False
    if (wanted.scheme, wanted.netloc) != (current.scheme, current.netloc):
        return False
    wanted_identity = (wanted.scheme, wanted.netloc, wanted.path, wanted.query)
    for element in obs.elements:
        if element.role != "link":
            continue
        href = element.attrs.get("href")
        if not isinstance(href, str) or href.lower().startswith("javascript:"):
            continue
        try:
            target = urlsplit(urljoin(obs.url, href))
        except ValueError:
            continue
        if (target.scheme, target.netloc, target.path, target.query) == wanted_identity:
            return True
    return False
