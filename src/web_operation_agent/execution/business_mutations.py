"""Confirmed typed business mutations and their fresh-page settlement."""
from __future__ import annotations

from ..business.write_service import BusinessWriteService
from ..transport.browser import TransportError, TurnCancelled
from ..business.errors import (BusinessMutationBlockedError, BusinessMutationIndeterminate,
                               BusinessMutationRefusedError, BusinessToolError,
                               BusinessToolPermissionError)
from .grounding import WRITE_UNKNOWN
from ..schemas import ActionReceipt, PageObservation
from .prompts import _assistant_call, _tool_result, _trim
from .runstate import _Next, _Turn


def execute_business_mutation(session, turn: "_Turn", call: dict, name: str, args: dict,
                              operation_context, operation_identity: dict) -> "_Next":
    result, t0, step = turn.result, turn.started_at, turn.step
    messages, obs = turn.messages, turn.obs
    successful_business_tools = turn.successful_business_tools
    service = BusinessWriteService(session.business_tools, session._approvals,
                                  session._loaded_business_skill_ids)
    try:
        supporting = ""
        if session.business_tools.require_skill_for_writes:
            if args.get("intent") != "execute" or not isinstance(args.get("supporting_skill"), str):
                from ..business.errors import BusinessToolRequestError
                raise BusinessToolRequestError("write requires execute intent and a loaded supporting Skill")
            supporting = args["supporting_skill"]
            args = {k: v for k, v in args.items() if k not in {"intent", "supporting_skill"}}
        proposal = service.prepare(name, args, context=operation_context, skill_id=supporting)
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

    approval = service.approve(
        proposal, step=step, context=operation_context,
        identity=operation_identity)
    if not approval.approved:
        return _Next(session._finish(
            result, t0, approval.reason, approval.detail, steps=step))
    approval_id = approval.approval_id
    try:
        tool_result = service.dispatch(proposal, approval, step=step,
            context=operation_context, identity=operation_identity)
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
    # A write changes what a read may return; a read after it is a check, not a repeat.
    session._read_digests.clear()
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
    session._form_draft_verification = None
    # A successful write invalidates values read before it. The receipt
    # remains authoritative even when this adapter supplies no new fields.
    session.evidence.discard_runtime()
    if not hasattr(session, "_mutation_receipts"):
        session._mutation_receipts = []
    session._mutation_receipts.append({"effect": proposal.effect, "targets": list(proposal.targets),
                                      "result_digest": tool_result.content_digest, "verified": True})
    session._answer_action_receipt = {
        "kind": "mutation", "effect": proposal.effect,
        "targets": list(proposal.targets), "completed": list(session._mutation_receipts)}
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
        session.ledger.append("business_mutation_followup_failed", step=step, tool=name,
                              completed=True, proposal_digest=proposal.content_digest,
                              error_type=type(exc).__name__, side_effect_possible=False)
        return _Next(session._finish(
            result, t0, "observation_failed",
            "business mutation completed but fresh page observation failed: "
            + str(exc), steps=step))
    # Deliberately not `WriteWatch.settle_write`: the typed acknowledgement and the
    # durable journal already agree this mutation completed, so stashing
    # `uncertain_write` here would downgrade a known write to an unknown
    # one and invite reconciliation for something already settled.
    if write["state"] in (WRITE_UNKNOWN, "control_error"):
        reason = ("observation_failed" if write["state"] == WRITE_UNKNOWN
                  else "control_plane_error")
        return _Next(session._finish(
            result, t0, reason, write["detail"], steps=step))
    if not fresh:
        return _Next(session._finish(
            result, t0, "observation_failed",
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
