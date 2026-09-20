"""The business surface of a step: typed reads, the one confirmed write, Skill, form.

These tools answer from the application itself rather than from the page: a typed read,
a mutation whose approval the watch granted, a loaded Skill card, and the application's
own form draft. Routing and page observation stay with the session and the page-action
path; what a business tool means lives here (P7.5 D-01).
"""
from __future__ import annotations

import hashlib
import json
import re

from ..answer import answer_protocol
from ..answer.answer_view import render_payload
from ..answer.evidence import EvidenceUnknown, TurnEvidence
from ..browser import TransportError, TurnCancelled
from ..business.errors import (BusinessMutationBlockedError, BusinessMutationIndeterminate,
                               BusinessMutationRefusedError, BusinessToolError,
                               BusinessToolPermissionError)
from ..business.runtime import FormDraft
from ..grounding import WRITE_UNKNOWN
from ..schemas import ActionReceipt, PageObservation
from .prompts import CLARIFICATION_CHOICE_LIMIT, _assistant_call, _tool_result, _trim
from .routes import observation_is_at_route
from .runstate import _Next, _Turn


def business_tool(session, turn: "_Turn", call: dict, name: str, args: dict) -> "_Next":
    """One typed business tool: a read, or the one confirmed write a turn may make.

    Its own locals are the turn's state, restored on the way out: this was the largest
    branch of the step loop, and lifting it whole is what made the loop readable without
    rewriting what a business tool does (P7.5 D-01).
    """
    result, t0, step = turn.result, turn.started_at, turn.step
    messages, obs = turn.messages, turn.obs
    successful_business_tools = turn.successful_business_tools
    operation_context = session._approvals.context(step, name)
    operation_identity = (operation_context.audit_projection()
                          if operation_context is not None else {})
    if session.business_tools.is_mutation(name):
        session._answer_action_receipt = None
        try:
            proposal = session.business_tools.prepare_mutation(
                name, args, session._loaded_business_skill_ids,
                context=operation_context)
        except BusinessToolPermissionError as exc:
            # The actor's authority answered, and it is final for this turn.
            # Handing it back to the model would only invite a page route to the
            # same effect, which is exactly the downgrade that must not happen.
            return _Next(session._permission_denied(
                result, t0, step, name, exc, dispatched=False,
                identity=operation_identity))
        except BusinessMutationRefusedError as exc:
            detail = str(exc)[:500]
            session.ledger.append(
                "business_mutation_rejected", step=step, tool=name,
                error_code=exc.code, reason=exc.reason,
                detail=detail, dispatched=False, side_effect_possible=False,
                identity=operation_identity)
            result.refusal_subject = exc.subject or None
            return _Next(session._finish(
                result, t0, "business_mutation_refused", exc.reason, steps=step))
        except BusinessToolError as exc:
            detail = str(exc)[:500]
            session.ledger.append(
                "business_mutation_rejected", step=step, tool=name,
                error_code=getattr(exc, "code", "business_tool"),
                reason=detail, dispatched=False, side_effect_possible=False,
                identity=operation_identity)
            messages += [_assistant_call(call), _tool_result(
                call, "BUSINESS MUTATION REFUSED BEFORE DISPATCH: " + detail)]
            messages = _trim(messages, session.keep_observations)
            return session._resume(turn, messages, obs, successful_business_tools)

        approval = session._approvals.approve_mutation(
            proposal, step=step, tool=name, context=operation_context,
            identity=operation_identity)
        if not approval.approved:
            return _Next(session._finish(
                result, t0, approval.reason, approval.detail, steps=step))
        approval_id = approval.approval_id
        def mutation_claimed() -> None:
            session._approvals.mutation_claimed(
                proposal, step=step, tool=name, approval_id=approval_id,
                identity=operation_identity)
        try:
            tool_result = session.business_tools.execute_mutation(
                proposal, approval.approved_digest, session._loaded_business_skill_ids,
                context=operation_context, on_claimed=mutation_claimed)
        except BusinessMutationBlockedError as exc:
            detail = str(exc)[:500]
            session.ledger.append(
                "business_mutation_blocked", step=step, tool=name,
                approval_id=approval_id,
                proposal_digest=proposal.content_digest,
                error_code=exc.code, reason=exc.reason, state=exc.state,
                detail=detail, dispatched=False,
                side_effect_possible=False, identity=operation_identity)
            return _Next(session._finish(
                result, t0, "business_mutation_blocked", detail, steps=step))
        except BusinessMutationIndeterminate as exc:
            session._approvals.mutation_indeterminate()
            detail = str(exc)[:500]
            # The digest names the journal row an operator has to settle, and
            # the reconciliation names what the application already learned about
            # it. Without both, settling a row means scanning the collection.
            session.ledger.append(
                "business_mutation_indeterminate", step=step, tool=name,
                approval_id=approval_id, reason=detail,
                proposal_digest=proposal.content_digest,
                reconciliation=exc.reconciliation,
                side_effect_possible=True, identity=operation_identity)
            return _Next(session._finish(
                result, t0, "indeterminate_write", detail, steps=step))
        except BusinessToolPermissionError as exc:
            # The application received the request and refused it itself; its
            # refusal is definite, so this is not an uncertain write.
            return _Next(session._permission_denied(
                result, t0, step, name, exc, dispatched=True,
                identity=operation_identity, approval_id=approval_id))
        except BusinessToolError as exc:
            detail = str(exc)[:500]
            session.ledger.append(
                "business_mutation_failed", step=step, tool=name,
                approval_id=approval_id,
                error_code=getattr(exc, "code", "business_tool"), reason=detail,
                side_effect_possible=False, identity=operation_identity)
            return _Next(session._finish(
                result, t0, "business_mutation_failed", detail, steps=step))
        session._approvals.mutation_completed()
        session.ledger.append(
            "business_mutation_result", step=step, tool=name,
            approval_id=approval_id,
            proposal_digest=proposal.content_digest,
            result_digest=tool_result.content_digest,
            audit=tool_result.audit,
            data_classes=list(tool_result.data_classes),
            route_count=len(tool_result.allowed_routes),
            side_effect_possible=True, completed=True,
            identity=operation_identity)
        session._business_routes.update(tool_result.allowed_routes)
        session.latest_business_tool_result = tool_result
        session._form_draft = None
        # A successful write invalidates values read before it. The receipt
        # remains authoritative even when this adapter supplies no new fields.
        session.evidence = TurnEvidence()
        session._answer_action_receipt = {
            "kind": "mutation", "effect": proposal.effect,
            "targets": list(proposal.targets)}
        session._answers.collect_evidence(tool_result, step=step)
        session._latest_business_query_page_actions = (
            session.page_actions if tool_result.query_answer is not None else None)
        session._feed.note(stage="write_done", tool=name)
        successful_business_tools += 1
        feedback = ("BUSINESS MUTATION RESULT (approved, executed once, "
                    "current runtime fact):\n" + tool_result.render())
        if session._evidence_feedback:
            feedback += "\n\n" + session._evidence_feedback
        if session._clarification_feedback:
            feedback += "\n\n" + session._clarification_feedback
        try:
            receipt, write, fresh = observe_after_business_mutation(
                session, obs, step, name)
        except TurnCancelled as exc:
            # The typed application acknowledgement and durable journal already
            # agree that this mutation completed. Cancelling the subsequent page
            # refresh ends the turn, but cannot turn a known write into an unknown
            # one or invite reconciliation/retry.
            detail = (f"{name} completed, but the follow-up page observation "
                      "was cancelled.")
            result.agent_said = detail
            session.ledger.append(
                "business_mutation_followup_cancelled", step=step, tool=name,
                proposal_digest=proposal.content_digest,
                result_digest=tool_result.content_digest,
                completed=True, dispatched=True,
                side_effect_possible=True, reason=str(exc)[:300])
            return _Next(session._finish(
                result, t0, "user_cancelled", detail, steps=step))
        except TransportError as exc:
            return _Next(session._finish(
                result, t0, "indeterminate_write",
                "business mutation completed but fresh page observation failed: "
                + str(exc), steps=step))
        # Deliberately not `WriteWatch.settle_write`: the typed acknowledgement and the
        # durable journal already agree this mutation completed, so stashing
        # `uncertain_write` here would downgrade a known write to an unknown
        # one and invite reconciliation for something already settled.
        if write["state"] in (WRITE_UNKNOWN, "control_error"):
            reason = ("indeterminate_write" if write["state"] == WRITE_UNKNOWN
                      else "control_plane_error")
            return _Next(session._finish(
                result, t0, reason, write["detail"], steps=step))
        if not fresh:
            return _Next(session._finish(
                result, t0, "indeterminate_write",
                f"{name} completed but no fresh observation was available",
                steps=step))
        obs = receipt.observation
        note = session._ground(
            obs, receipt, write["state"], write["refused_requests"])
        session._sync_plan_message(messages)
        messages += [_assistant_call(call), _tool_result(
            call, feedback + "\n\n" + session._render(
                obs, receipt, note, write))]
        messages = _trim(messages, session.keep_observations)
        return session._resume(turn, messages, obs, successful_business_tools)

    tool_result = None
    if session._cancel_requested():
        return _Next(session._finish(
            result, t0, "user_cancelled",
            "the user stopped this task before tool dispatch", steps=step))
    try:
        tool_result = session.business_tools.execute(
            name, args, session._loaded_business_skill_ids,
            context=operation_context)
        session.ledger.append(
            "business_tool_result", step=step, tool=name,
            result_digest=tool_result.content_digest,
            audit=tool_result.audit,
            data_classes=list(tool_result.data_classes),
            route_count=len(tool_result.allowed_routes),
            **({"navigation_target_digest": hashlib.sha256(
                tool_result.navigation_target.encode()).hexdigest()}
               if tool_result.navigation_target else {}),
            side_effect_possible=False, identity=operation_identity)
        session._business_routes.update(tool_result.allowed_routes)
        session._latest_business_routes = set(tool_result.allowed_routes)
        session.latest_business_tool_result = tool_result
        session._form_draft = (
            tool_result.form_draft if session.allow_form_prefill else None)
        recorded_evidence = session._answers.collect_evidence(tool_result, step=step)
        session._latest_business_query_page_actions = (
            session.page_actions if tool_result.query_answer is not None else None)
        if tool_result.query_answer is not None:
            session._feed.note(
                stage="read_done", tool=name,
                count=tool_result.query_answer.matched_count,
                scope=session._scope_label(tool_result.query_answer.scope))
        successful_business_tools += 1
        feedback = ("BUSINESS TOOL RESULT (current runtime fact):\n"
                    + tool_result.render())
        if session._form_draft is not None:
            feedback += (
                "\n\nAPPLICATION FORM DRAFT READY: Call prefill_form with no "
                "arguments. The server will use the exact current route, fields "
                "and values above, stop before submission, and hand control to "
                "the user.")
        if session._evidence_feedback:
            feedback += "\n\n" + session._evidence_feedback
        if session._clarification_feedback:
            feedback += "\n\n" + session._clarification_feedback
        if tool_result.query_answer is not None:
            feedback += (
                "\n\nSERVER QUERY ANSWER READY: For a list or filtered lookup, "
                "answer now without a browser action. Navigate only when the "
                "user explicitly asked to open a record or collection.")
            projection = tool_result.query_answer
            if (projection.complete and projection.matched_count == 0
                    and all(skill.read_only
                            for skill in session._task_business_skills)
                    and len(recorded_evidence) == 1):
                evidence_ref = recorded_evidence[0]
                try:
                    verified = answer_protocol.submit(session.evidence, {
                        "outcome": "answered",
                        "answer": {
                            "version": 1,
                            "coverage": "complete",
                            "presentation": "brief",
                            "conclusions": [{
                                "kind": "not_found",
                                "evidence_ref": evidence_ref,
                            }],
                        },
                    })
                except (ValueError, TypeError, EvidenceUnknown):
                    # An adapter may provide other evidence shapes beside its
                    # display projection. In that case the model still has to
                    # choose a conclusion; never guess which item proves absence.
                    pass
                else:
                    verified = session._answers.with_labels(verified)
                    result.verified_answer = verified
                    result.agent_said = render_payload(
                        verified, user_text=session.task.prompt,
                        max_chars=max(512, session.max_agent_said_chars))
                    session.ledger.append(
                        "answer_verified", step=step, conclusions=1,
                        coverage="complete", deterministic="empty_query")
                    return _Next(session._finish(
                        result, t0, "agent_finished", result.agent_said,
                        steps=step))
            # Several records answer the person's own identifying words only
            # when the read carried them. A read that named nothing returns the
            # collection, and "which one did you mean" is then a question they
            # did not ask: "打开金额最高的销售订单" was answered with a list of
            # every order. That request stays with the model, which can compute
            # the one it names.
            identified = any(getattr(item, "looked_for", "")
                             for item in tool_result.evidence or ())
            if (session.allow_clarification and session._navigation_only and identified
                    and projection.complete
                    and 1 < projection.matched_count
                    <= CLARIFICATION_CHOICE_LIMIT
                    and len(session._clarification_choices)
                    == projection.matched_count
                    and all(skill.read_only
                            for skill in session._task_business_skills)):
                # The current typed read already proved both the ambiguity and
                # the complete set of choices. Asking the model to copy those
                # choices into ask_user adds latency and lets it omit or rename
                # records without adding judgment. Keep this transition in the
                # server contract; the reply still grants no route authority and
                # the next turn must read the application again before opening.
                # A set larger than one question can offer stays with the model,
                # which asks the person to narrow it in its own words.
                question = (
                    "找到多个匹配项，请选择要打开的记录："
                    if re.search(r"[\u3400-\u9fff]", session.task.prompt)
                    else "Multiple records match. Choose the one to open:")
                return _Next(session._finish_with_clarification(
                    result, t0, step=step, question=question,
                    choice_ids=list(session._clarification_choices),
                    deterministic="complete_navigation_query"))
        # A request that names a second read collection may want a related
        # record's page ("the customer of order S1"). The record this read found is
        # then only the way there, so the model, not this shortcut, decides what
        # to open.
        if (session.deterministic_read_navigation and session._navigation_only
                and sum(skill.read_only for skill in session._task_business_skills) == 1
                and "goto" not in session.disabled_actions
                and tool_result.navigation_target):
            target = tool_result.navigation_target
            if session._cancel_requested():
                return _Next(session._finish(
                    result, t0, "user_cancelled",
                    "the user stopped this task before browser dispatch",
                    steps=step))
            reason, detail = session._approvals.confirm_navigation(
                step, obs, action="goto", url=target)
            if reason:
                return _Next(session._finish(result, t0, reason, detail, steps=step))
            prior_page_epoch = obs.epoch
            try:
                session._browser_attempts += 1
                receipt = session.browser.execute(
                    "goto", {"url": target}, expect_navigation=True)
            except TurnCancelled as exc:
                session.ledger.append(
                    "browser_action_cancelled_before_dispatch", step=step,
                    tool="goto", dispatched=False, side_effect_possible=False)
                return _Next(session._finish(
                    result, t0, "user_cancelled", str(exc), steps=step))
            except TransportError as exc:
                return _Next(session._finish(
                    result, t0, session._transport_terminal(
                        "goto", may_write=False), str(exc), steps=step))
            session._approvals.settle_navigation(receipt)
            if receipt.error_code != "stale_observation":
                session.page_actions += 1
            write = session._writes.write_outcome(
                receipt, step, action_may_write=False)
            session.ledger.append(
                "action_receipt", step=step, tool=receipt.action,
                command_id=receipt.command_id,
                write_attribution="unattributed", ok=receipt.ok,
                error_code=receipt.error_code, navigated=receipt.navigated,
                url_after=receipt.url_after, elapsed_ms=receipt.elapsed_ms,
                write=write["state"], relogin=receipt.relogin,
                deterministic_business_navigation=True)
            if receipt.error_code == "stale_observation":
                # The command did not reach the page and the receipt carries a
                # replacement observation. Repeating the decision against that
                # observation is recovery, not a no-progress loop.
                session._repeats.reset()
            reason, detail = session._writes.settle_write(write)
            if reason:
                return _Next(session._finish(result, t0, reason, detail, steps=step))
            if receipt.observation is not None:
                obs = receipt.observation
            reached = bool(
                receipt.ok and receipt.observation is not None
                and receipt.observation.epoch > prior_page_epoch
                and observation_is_at_route(receipt.observation, target))
            if reached:
                session._answer_action_receipt = {
                    "kind": "navigation", "url": obs.url}
                result.agent_said = (
                    "已打开请求的页面。"
                    if re.search(r"[\u3400-\u9fff]", session.task.prompt)
                    else "Opened the requested page.")
                session.ledger.append(
                    "deterministic_navigation_completed", step=step,
                    url_digest=hashlib.sha256(obs.url.encode()).hexdigest(),
                    observation_epoch=obs.epoch, side_effect_possible=False,
                    source="business_result")
                return _Next(session._finish(
                    result, t0, "agent_finished", result.agent_said,
                    steps=step))
            note = session._ground(
                obs, receipt, write["state"], write["refused_requests"])
            feedback += (
                "\n\nThe server tried the adapter's unique current route, but "
                "the fresh page did not verify that target. Replan from the "
                "current page.\n\n" + session._render(obs, receipt, note, write))
    except BusinessToolPermissionError as exc:
        # The account's own authority refused this read. The turn goes on with
        # what it already read -- ending it threw away the half of the question
        # this account may see -- but the refusal closes the page as a source of
        # facts, because handing the refusal back with the page still readable is
        # how a refused scope gets read off the screen instead (owner ruling
        # D13). From here every answer must be typed and evidence-backed, page
        # evidence is discarded and refused, and the answer says what was not
        # read whether or not the model chooses to.
        label = session._answers.operation_label(name)
        detail = str(exc)[:500]
        session.ledger.append(
            "business_tool_permission_denied", step=step, tool=name,
            error_code=exc.code, layer=exc.layer,
            required_scope=exc.required_scope,
            dispatched=exc.layer == "application", turn_continued=True,
            side_effect_possible=False, identity=operation_identity)
        if label not in session._refused_reads:
            # An adapter with no name for it in this language still records the
            # refusal, so the answer cannot read as a whole answer.
            session._refused_reads.append(label)
        session._refused_detail = detail
        result.refused_operation = label or result.refused_operation
        session._answer_mode = True
        session.evidence.discard_pages()
        session._page_answer_digest = ""
        session._form_draft = None
        session._latest_business_routes = set()
        session._clarification_choices = {}
        session._clarification_feedback = ""
        feedback = (
            "BUSINESS TOOL REFUSED BY AUTHORIZATION: " + detail +
            " This account may not perform that read, and no other route to the "
            "same facts is open in this turn: page content is no longer accepted "
            "as evidence. Answer only from the typed reads you already have, and "
            "state what you could not read as an undetermined conclusion. If you "
            "have read nothing, finish with outcome=unable.")
    except BusinessToolError as exc:
        session.latest_business_tool_result = None
        session._form_draft = None
        session._latest_business_routes = set()
        session._clarification_choices = {}
        session._clarification_feedback = ""
        session._latest_business_query_page_actions = None
        detail = str(exc)[:500]
        session.ledger.append(
            "business_tool_rejected", step=step, tool=name,
            error_code=getattr(exc, "code", "business_tool"),
            reason=detail, side_effect_possible=False,
            identity=operation_identity)
        feedback = "BUSINESS TOOL FAILED: " + detail
    session._sync_plan_message(messages)
    messages += [_assistant_call(call), _tool_result(call, feedback)]
    messages = _trim(messages, session.keep_observations)
    return session._resume(turn, messages, obs, successful_business_tools)


def load_skill(session, turn: "_Turn", call: dict, name: str, args: dict) -> "_Next":
    """Load one of this task's candidate Business Skills: a playbook, not runtime data."""
    skill, rejection = session.business_tools.load(
        args.get("skill_id"), session._task_business_skill_ids)
    if rejection:
        session.ledger.append(
            "business_skill_rejected", step=turn.step,
            skill_id=str(args.get("skill_id") or "")[:200], reason=rejection,
            side_effect_possible=False)
        return session._reply(turn, call, "The server rejected load_skill: " + rejection + ".")
    assert skill is not None
    session._loaded_business_skill_ids.add(skill.skill_id)
    session.ledger.append(
        "business_skill_loaded", step=turn.step, skill_id=skill.skill_id,
        skill_sha256=skill.artifact_sha256,
        tool_names=list(skill.tool_names), provenance=list(skill.provenance))
    return session._reply(turn, call,
                       "BUSINESS SKILL LOADED (playbook; not runtime data):\n"
                       + json.dumps(skill.loaded_card(), ensure_ascii=False,
                                    sort_keys=True, separators=(",", ":")))


def prefill_form(session, turn: "_Turn", call: dict, name: str, args: dict) -> "_Next":
    """Fill the application's own draft into its form, and hand the page to the person.

    The turn ends here whatever happens: a prefilled form is the person's to check and
    save, and nothing the assistant does afterwards would be about it.
    """
    if args:
        return session._reply(turn, call,
                           "prefill_form accepts no arguments; route, fields and values "
                           "come only from the current application-issued draft.")
    draft = session._form_draft
    # Consume before dispatch. Even though the application contract says these fields do
    # not persist until user submission, repeating a partially applied draft after
    # transport loss would be surprising and could retrigger onchange.
    session._form_draft = None
    if draft is None:
        return session._reply(turn, call,
                           "No current application-issued form draft is available.")
    if draft.needs_confirmation:
        reason, detail = session._approvals.confirm_form_handoff(draft, turn.step)
        if reason:
            return _Next(session._finish(turn.result, turn.started_at, reason, detail,
                                      steps=turn.step))
    prefilled, reason, detail = apply_form_draft(session, draft, turn.obs, turn.step)
    if prefilled is not None:
        turn.obs = prefilled
    chinese = bool(re.search(r"[\u3400-\u9fff]", session.task.prompt))
    if reason:
        turn.result.agent_said = (
            "表单未能完整预填，未执行提交。" if chinese
            else "The form could not be fully prefilled and was not submitted.")
        return _Next(session._finish(
            turn.result, turn.started_at, reason,
            turn.result.agent_said + (f" {detail}" if detail else ""), steps=turn.step))
    if draft.needs_confirmation:
        # This one the application commits by itself once the page is left, so the
        # sentence says that plainly rather than "may save or discard".
        turn.result.agent_said = (
            "已按你的确认填好，尚未保存。请在页面上核对后自己保存；"
            "离开这个页面时，应用会保存这些改动。"
            if chinese
            else "Filled as you confirmed and not saved yet. Check it on the page and "
                 "save it yourself; leaving this page lets the application save these "
                 "changes.")
    else:
        turn.result.agent_said = (
            "表单已预填，尚未提交。请检查或修改后由你继续。"
            "离开这个页面时，应用可能会自动保存或丢弃这些内容。"
            if chinese
            else "The form is prefilled and has not been submitted. Review or edit it, "
                 "then continue yourself. Leaving this page may let the application save "
                 "or discard it.")
    session.ledger.append(
        "form_prefill_completed", step=turn.step,
        draft_digest=draft.content_digest,
        route_digest=hashlib.sha256(draft.route.encode()).hexdigest(),
        fields=[item.field for item in draft.fields],
        submitted=False, persisted=False, user_takeover=True)
    return _Next(session._finish(turn.result, turn.started_at, "agent_finished",
                              turn.result.agent_said, steps=turn.step))


def observe_after_business_mutation(session, obs: PageObservation, step: int,
                                     server_tool: str) -> tuple[ActionReceipt, dict, bool]:
    """Return one fresh page after a completed typed business mutation."""
    if session._cancel_requested():
        raise TurnCancelled("the user stopped this task before page observation")
    prior_epoch = obs.epoch
    session._browser_attempts += 1
    receipt = session.browser.execute("observe", {})
    write = session._writes.write_outcome(receipt, step, action_may_write=False)
    observed_epoch = receipt.observation.epoch if receipt.observation is not None else None
    fresh = bool(receipt.action == "observe" and receipt.ok
                 and receipt.observation is not None and observed_epoch > prior_epoch)
    session.ledger.append(
        "action_receipt", step=step, tool="observe",
        command_id=receipt.command_id, after_tool=server_tool,
        server_tool_dispatched=True, browser_action_dispatched=True,
        side_effect_possible=False, write_attribution="unattributed",
        ok=receipt.ok, error_code=receipt.error_code,
        navigated=receipt.navigated, url_after=receipt.url_after,
        elapsed_ms=receipt.elapsed_ms, write=write["state"], relogin=receipt.relogin,
        page_epoch_before=prior_epoch, observation_epoch=observed_epoch,
        fresh_observation=fresh)
    return receipt, write, fresh


def apply_form_draft(
        session, draft: FormDraft, observation: PageObservation,
        step: int) -> tuple[PageObservation | None, str, str]:
    """Apply one adapter-issued transient draft without exposing its bindings to choice.

    The application has attested that these exact fields do not persist until a user
    submits. This method never calls ``click`` or ``submit`` and every generic browser
    action still yields a fresh observation before the next field is resolved.
    """
    current = observation
    #: The error code the last dispatched command came back with, so the caller can
    #: tell "the page moved while we were aiming" from a real refusal.
    last_code = ""
    actions: list[tuple[str, dict, str]] = []
    if not observation_is_at_route(current, draft.route):
        actions.append(("goto", {"url": draft.route}, ""))

    def dispatch(action: str, args: dict, field: str,
                 before: PageObservation) -> tuple[PageObservation | None, str, str]:
        if session._cancel_requested():
            return before, "user_cancelled", "the user stopped before form preparation"
        prior_epoch = before.epoch
        nonlocal last_code
        last_code = ""
        try:
            session._browser_attempts += 1
            receipt = session.browser.execute(action, args)
        except TurnCancelled as exc:
            return before, "user_cancelled", str(exc)
        except TransportError as exc:
            return before, "transport_error", str(exc)
        if action not in ("observe", "wait_for"):
            session._approvals.settle_navigation(receipt)
        if action != "observe":
            session.page_actions += 1
        write = session._writes.write_outcome(receipt, step, action_may_write=False)
        session.ledger.append(
            "form_prefill_action", step=step, tool=action,
            command_id=receipt.command_id, field=field,
            value_digest=(hashlib.sha256(str(args.get("value", "")).encode()).hexdigest()
                          if field else ""),
            ok=receipt.ok, error_code=receipt.error_code,
            page_epoch_before=prior_epoch,
            observation_epoch=(receipt.observation.epoch
                               if receipt.observation is not None else None),
            side_effect_possible=False, persisted=False, submitted=False,
            write=write["state"])
        terminal, detail = session._writes.settle_write(write)
        if terminal:
            return receipt.observation or before, terminal, detail
        last_code = str(receipt.error_code or "")
        if (not receipt.ok or receipt.observation is None
                or receipt.observation.epoch <= prior_epoch):
            return (receipt.observation or before, "agent_refused",
                    f"{action} did not return a successful fresh page")
        after = receipt.observation
        if not observation_is_at_route(after, draft.route):
            return after, "agent_refused", "the application left the issued form route"
        return after, "", ""

    for action, args, field in actions:
        if action == "goto":
            reason, detail = session._approvals.confirm_navigation(
                step, current, action="goto", url=args["url"])
            if reason:
                return current, reason, detail
        current, reason, detail = dispatch(action, args, field, current)
        if reason:
            return current, reason, detail

    for binding in draft.fields:
        matches = [element for element in current.elements
                   if element.role == binding.control
                   and element.attrs.get("field") == binding.field]
        # A SPA may commit its URL before the component tree for the new route is ready.
        # Do not let a stale first observation turn an issued draft into an arbitrary
        # selector fallback. Re-observe after bounded, short waits and keep requiring the
        # same exact semantic field from the fresh page.
        for wait_ms in (100, 250, 500):
            if matches:
                break
            current, reason, detail = dispatch(
                "wait_for", {"ms": wait_ms}, "", current)
            if reason:
                return current, reason, detail
            matches = [element for element in current.elements
                       if element.role == binding.control
                       and element.attrs.get("field") == binding.field]
        if len(matches) != 1:
            session.ledger.append(
                "form_prefill_field_refused", step=step, field=binding.field,
                matches=len(matches),
                available_fields=[{
                    "field": str(element.attrs.get("field") or "")[:80],
                    "role": element.role,
                } for element in current.elements
                    if element.attrs.get("field")],
                dispatched=False, side_effect_possible=False)
            return current, "agent_refused", (
                f"the current form did not expose one exact {binding.field} field")
        action = "select" if binding.control == "select" else "fill"
        # The application finishes rendering its own form while we are aiming at it, so
        # the first fill can be refused as stale -- which is the runtime doing its job:
        # nothing was typed. Look again and aim at the field the fresh page has. This is
        # what left an issued draft reported as "表单未能完整预填" on a page where the
        # fields were plainly there.
        for attempt in range(3):
            current, reason, detail = dispatch(
                action,
                {"ref": matches[0].ref, "epoch": current.epoch, "value": binding.value},
                binding.field, current)
            if not reason:
                break
            if last_code != "stale_observation" or attempt == 2:
                return current, reason, detail
            current, reason, detail = dispatch("observe", {}, "", current)
            if reason:
                return current, reason, detail
            matches = [element for element in current.elements
                       if element.role == binding.control
                       and element.attrs.get("field") == binding.field]
            if len(matches) != 1:
                session.ledger.append(
                    "form_prefill_field_refused", step=step, field=binding.field,
                    matches=len(matches), dispatched=False, side_effect_possible=False)
                return current, "agent_refused", (
                    f"the current form did not expose one exact {binding.field} field")
    return current, "", ""


def render_business_skills(session) -> str:
    if not session._task_business_skills:
        return ""
    if not session._auto_loaded_business_skill_ids:
        cards = [skill.candidate_card(loaded=False)
                 if skill.skill_id not in session._loaded_business_skill_ids
                 else skill.candidate_card(loaded=True)
                 for skill in session._task_business_skills]
        return ("BUSINESS SKILL CANDIDATES (load before using their tools):\n"
                + json.dumps(cards, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")))
    cards = [
        skill.loaded_card()
        if skill.skill_id in session._auto_loaded_business_skill_ids
        else skill.candidate_card(loaded=False)
        for skill in session._task_business_skills]
    return ("BUSINESS SKILLS (loaded cards include their playbook; load remaining "
            "candidates before using their tools):\n"
            + json.dumps(cards, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")))
