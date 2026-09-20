"""The browser path of a step: refusal, routing, dispatch, receipt, write settlement.

A page action is the path the browser is on, and the routing facts it establishes -- an
exact source route, a skill's application entry, whether the route is read-only -- are
its own. The tools that answer for themselves live with what they mean (P7.5 D-01).
"""
from __future__ import annotations

import hashlib
import re
import time
from urllib.parse import urljoin, urlsplit

from ..answer import answer_protocol, page_answer
from ..answer.evidence import EvidenceUnknown
from ..browser import TransportError, TurnCancelled
from ..business.writes import WRITE_CAPABLE
from ..grounding import WRITE_PERMITTED
from ..privacy import redact_sensitive_assignments, safe_tool_arguments as _safe
from ..schemas import BROWSER_ACTIONS, ActionReceipt, PageObservation
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
    if name in session.disabled_actions:
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
            f"The run's action policy blocked {name!r} before dispatch. "
            "The browser did not receive that requested action and it could not "
            "change the application. Inspect the fresh page and choose another "
            "available action.\n\n" + session._render(obs, receipt, "", write))
        messages += [_assistant_call(call), _tool_result(call, feedback)]
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
        session.ledger.append("source_route_opened", step=step, url=str(url)[:500],
                           citations=source_route_citations)
        source_route_read_only = True
        name, args = "goto", {"url": url}

    if name == "check_capability" and session._capability_check_available():
        messages += [_assistant_call(call),
                     _tool_result(call, session._capability_check_feedback())]
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
                side_effect_possible=False)
        elif not isinstance(url, str) or not _page_link_allows(url, obs):
            messages += [_assistant_call(call), _tool_result(
                call, "Direct navigation is allowed only to an exact link on the "
                      "current page, an exact ui_path from a successful business "
                      "ToolResult, or a cited source route.")]
            session.ledger.append("page_route_rejected", step=step,
                               url=str(url or "")[:500], page_url=obs.url)
            return session._resume(turn, messages, obs, successful_business_tools)

    action_may_write = name in WRITE_CAPABLE and not source_route_read_only
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
    if name in ("goto", "back") or (name == "click" and (expect_nav or link is not None)):
        reason, detail = session._approvals.confirm_navigation(
            step, obs, action=name,
            url=(str(args.get("url") or "") if name == "goto" else (link or "")))
        if reason:
            return _Next(session._finish(result, t0, reason, detail, steps=step))
    prior_page_epoch = obs.epoch
    try:
        session._browser_attempts += 1
        session._writes.expect_decision(action_may_write)
        preserve_completed_write = bool(
            session._answer_action_receipt is not None
            and session._answer_action_receipt["kind"] == "mutation"
            and name in ("goto", "back"))
        if name not in ("observe", "wait_for") and not preserve_completed_write:
            session._answer_action_receipt = None
        receipt = session.browser.execute(name, args, expect_navigation=expect_nav)
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
        session._approvals.settle_navigation(receipt)
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
    if (name in ("goto", "back") and receipt.ok
            and receipt.observation is not None):
        if session._answer_action_receipt is not None and session._answer_action_receipt["kind"] == "mutation":
            session._answer_action_receipt["navigation_url"] = obs.url
        else:
            session._answer_action_receipt = {"kind": "navigation", "url": obs.url}
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
            and target_observed):
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
            session.evidence.discard_pages()
            session._page_answer_digest = obs.content_digest
            clean = PageObservation.from_json(
                redact_sensitive_assignments(session.session_handoffs.redact(obs.to_json())))
            rendered_observation = clean
            try:
                refs = session.evidence.capture(page_answer.collect(clean, observed_at=str(time.time())),
                                             namespace=f"obs{step}")
                page_feedback = (answer_protocol.ANSWER_INSTRUCTIONS + "\n" +
                                 answer_protocol.evidence_index(session.evidence, refs))
                session.ledger.append("page_answer_evidence", step=step, epoch=obs.epoch,
                                   recorded=len(refs), digest=obs.content_digest)
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


def _clicked_link(args: dict, obs: PageObservation) -> str | None:
    """The target of the link a click names, "" for a link without one, None for no link."""
    ref = args.get("ref")
    for element in obs.elements:
        if element.ref == ref and element.role == "link":
            href = element.attrs.get("href") if isinstance(element.attrs, dict) else None
            return href if isinstance(href, str) else ""
    return None


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
