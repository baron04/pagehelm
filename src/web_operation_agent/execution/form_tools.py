"""Application-issued form drafts and fixed generic browser form actions."""
from __future__ import annotations

import hashlib
import json
import re
import time

from ..transport.browser import TransportError, TurnCancelled
from ..answer.evidence import EvidenceUnknown, SourceEvidence
from ..answer.answer_view import form_handoff_answer
from ..business.errors import (BusinessToolError, BusinessToolPermissionError,
                               BusinessToolRequestError, BusinessToolContractError,
                               BusinessToolIdentityError)
from ..business.runtime import FormDraft
from ..business.tools.contracts import matches_form_control, form_control_unavailable
from ..schemas import PageObservation
from .routes import observation_is_at_route
from .page_evidence import capture_page_snapshot
from .runstate import _Next, _Turn
from .budget import TurnBudget
from ..modeling.spend import BudgetExhausted
from ..state.identity import PrincipalError


def _form_identity(session, step: int, tool: str) -> tuple[object, dict]:
    context = session._approvals.context(step, tool)
    return context, context.audit_projection() if context is not None else {}


def _source_bases(session, args: object) -> tuple[object, tuple[str, ...]]:
    """Validate source-derived value provenance without exposing it to an adapter.

    The check establishes that the cited bytes belong to this turn and deployment, not
    that the model's interpretation or arithmetic is correct. The current-form contract
    independently validates the proposed value against live fields and choices.
    """
    if not isinstance(args, dict) or not isinstance(args.get("values"), list):
        return args, ()
    values = []
    refs = []
    for item in args["values"]:
        if not isinstance(item, dict) or "source_basis" not in item:
            values.append(item)
            continue
        basis = item["source_basis"]
        if (not isinstance(basis, dict) or set(basis) != {"ref", "support"}
                or not isinstance(basis["ref"], str)
                or not isinstance(basis["support"], str)
                or not 8 <= len(basis["support"]) <= 400
                or not re.fullmatch(r"source#[1-9][0-9]*", basis["ref"])):
            raise BusinessToolRequestError("source_basis requires a cited source span")
        try:
            evidence = session.evidence.get(basis["ref"])
        except EvidenceUnknown:
            raise BusinessToolRequestError(
                "source_basis must cite source evidence from this turn") from None
        if (not isinstance(evidence, SourceEvidence)
                or evidence.release != session.source.release
                or (basis["support"] not in evidence.excerpt
                    and basis["support"] not in evidence.summary)):
            raise BusinessToolRequestError(
                "source_basis does not match this deployment's cited source")
        values.append({key: value for key, value in item.items()
                       if key != "source_basis"})
        refs.append(basis["ref"])
    return {**args, "values": values}, tuple(refs)


def describe_form(session, turn: "_Turn", call: dict, name: str, args: dict) -> "_Next":
    """Describe only the writable fields proven by both the page and the adapter."""
    session._form_draft = None
    session._form_draft_verification = None
    context, identity = _form_identity(session, turn.step, name)
    try:
        if session._form_entry_tools is not None:
            session._form_entry_tools.accept_current(turn.obs, context=context)
        content = session._application_form_tools.describe(
            turn.obs, args, context=context)
    except BusinessToolPermissionError as exc:
        return _Next(session._permission_denied(
            turn.result, turn.started_at, turn.step, name, exc,
            dispatched=exc.layer == "application", identity=identity,
            event="application_form_permission_denied"))
    except BusinessToolError as exc:
        session.ledger.append(
            "application_form_refused", step=turn.step, tool=name,
            error_type=type(exc).__name__, detail=str(exc)[:500],
            dispatched=False, side_effect_possible=False, identity=identity)
        return session._reply(turn, call, "FORM DESCRIPTION REFUSED: " + str(exc)[:500])
    encoded = json.dumps(content, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False)
    session.ledger.append(
        "application_form_described", step=turn.step, tool=name,
        result_digest=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        form_available=bool(content.get("form_available")),
        returned_fields=int(content.get("returned_field_count", 0)),
        side_effect_possible=False, identity=identity)
    return session._reply(
        turn, call, "CURRENT FORM DESCRIPTOR (runtime fact; opaque bindings):\n" + encoded)


def prepare_form(session, turn: "_Turn", call: dict, name: str, args: dict) -> "_Next":
    """Mint one exact adapter-validated draft from fresh opaque form bindings."""
    session._form_draft = None
    session._form_draft_verification = None
    context, identity = _form_identity(session, turn.step, name)
    try:
        form_args, source_refs = _source_bases(session, args)
        draft, content = session._application_form_tools.prepare(
            turn.obs, form_args, context=context)
        verification = session._application_form_tools.take_verification(draft)
    except BusinessToolPermissionError as exc:
        return _Next(session._permission_denied(
            turn.result, turn.started_at, turn.step, name, exc,
            dispatched=exc.layer == "application", identity=identity,
            event="application_form_permission_denied"))
    except BusinessToolError as exc:
        session.ledger.append(
            "application_form_refused", step=turn.step, tool=name,
            error_type=type(exc).__name__, detail=str(exc)[:500],
            dispatched=False, side_effect_possible=False, identity=identity)
        return session._reply(turn, call, "FORM PREPARATION REFUSED: " + str(exc)[:500])
    session._form_draft = draft
    session._form_draft_verification = verification
    encoded = json.dumps(content, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False)
    session.ledger.append(
        "application_form_prepared", step=turn.step, tool=name,
        draft_digest=draft.content_digest, field_count=len(draft.fields),
        source_refs=list(source_refs),
        persistence=draft.persistence, side_effect_possible=False,
        submitted=False, persisted=False, identity=identity)
    return session._reply(
        turn, call, "APPLICATION FORM DRAFT READY:\n" + encoded
        + "\nCall prefill_form with no arguments. The server retains the exact route, "
          "native fields and values; nothing has been typed or submitted yet.")


def prefill_form(session, turn: "_Turn", call: dict, name: str, args: dict) -> "_Next":
    """Fill the application's own draft into its form, and hand the page to the person.

    After a verified handoff, only any accompanying read-only questions may continue;
    the assistant cannot navigate away, fill again, or submit while completing them.
    """
    if args:
        return session._reply(turn, call,
                           "prefill_form accepts no arguments; route, fields and values "
                           "come only from the current application-issued draft.")
    draft = session._form_draft
    verification = session._form_draft_verification
    # Consume before dispatch. Even though the application contract says these fields do
    # not persist until user submission, repeating a partially applied draft after
    # transport loss would be surprising and could retrigger onchange.
    session._form_draft = None
    session._form_draft_verification = None
    if draft is None:
        return session._reply(turn, call,
                           "No current application-issued form draft is available.")
    if draft.needs_confirmation:
        reason, detail = session._approvals.confirm_form_handoff(draft, turn.step)
        if reason:
            return _Next(session._finish(turn.result, turn.started_at, reason, detail,
                                      steps=turn.step))
    # Inherit the loop's elapsed time rather than starting a new form deadline. The
    # generic loop excludes user approval waits, so this clock keeps that same policy.
    budget = session._turn_budget
    if budget is None:
        budget = TurnBudget(steps=1, seconds=session.wall_budget_s,
                            cancelled=session._cancel_requested,
                            clock=lambda: time.monotonic() - session._approvals.waited_ms / 1000)
        budget.started -= max(0.0, time.time() - turn.started_at
                              - session._approvals.waited_ms / 1000)
    prefilled, reason, detail = apply_form_draft(
        session, draft, turn.obs, turn.step, budget=budget,
        verify=(lambda obs: verification.check(draft, obs,
                context=session._approvals.context(turn.step, "prefill_form")))
               if verification is not None else None)
    if prefilled is not None:
        turn.obs = prefilled
    chinese = bool(re.search(r"[\u3400-\u9fff]", session.task.prompt))
    if reason:
        turn.result.agent_said = (
            "预填或交接未能核验；部分字段可能已经输入。请核对当前页面，助手没有执行提交。"
            if chinese else
            "Prefill or handoff could not be verified; some fields may have been entered. "
            "Check the current page. The assistant did not submit the form.")
        return _Next(session._finish(
            turn.result, turn.started_at, reason,
            turn.result.agent_said + (f" {detail}" if detail else ""), steps=turn.step))
    turn.result.agent_said = form_handoff_answer(user_text=session.task.prompt,
        needs_confirmation=draft.needs_confirmation, submit_label=draft.submit_label)
    session._answer_action_receipt = {"kind": "form_prefill", "url": prefilled.url,
        "needs_confirmation": draft.needs_confirmation, "submit_label": draft.submit_label}
    session.ledger.append(
        "form_prefill_completed", step=turn.step,
        draft_digest=draft.content_digest,
        route_digest=hashlib.sha256(draft.route.encode()).hexdigest(),
        fields=[item.field for item in draft.fields],
        submitted=False, persisted=False, user_takeover=True)
    if session._task_requirements is not None and session._task_requirements.answer_requests:
        session._finalizing = True
        session._answers.action_answer_required = True
        try:
            _, evidence_feedback = capture_page_snapshot(session, turn.obs, step=turn.step)
        except (ValueError, TypeError, EvidenceUnknown):
            evidence_feedback = "Use a fresh observation or registered read for missing facts."
        return session._reply(turn, call, turn.result.agent_said + "\n"
            "FORM PREFILL VERIFIED. Complete the frozen accompanying questions using "
            "only observation, registered reads, offered source lookup, compute and finish. No navigation, "
            "opening, filling or submission is permitted after this handoff. "
            "Report the observed form_prefill action with valid answers in finish, "
            "or preserve the handoff and unresolved questions in a partial result.\n"
            + evidence_feedback)
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
        step: int, *, budget: TurnBudget | None = None, verify=None
        ) -> tuple[PageObservation | None, str, str]:
    """Apply one adapter-issued transient draft without exposing its bindings to choice.

    The application has attested the persistence behavior of these exact fields. This
    method never calls ``click`` or ``submit`` and every generic browser action yields a
    fresh observation before the next field is resolved. Completion requires a browser
    receipt for the actual values and release of the read-only window.
    """
    current = observation

    def verified(before):
        if verify is None or before.render_state == "pending":
            return "", ""
        try:
            if verify(before) is not None:
                raise BusinessToolContractError("form verification returned an invalid verdict")
        except PrincipalError as exc:
            session.ledger.append("form_entry_verification_refused", step=step,
                                  error_code=BusinessToolIdentityError.code, dispatched=False,
                                  submitted=False, persisted=False)
            return "agent_refused", str(exc)
        except BusinessToolError as exc:
            session.ledger.append("form_entry_verification_refused", step=step,
                                  error_code=exc.code, dispatched=False,
                                  submitted=False, persisted=False)
            return ("permission_denied" if isinstance(exc, BusinessToolPermissionError)
                    else "agent_refused"), str(exc)
        return "", ""

    def stopped():
        try:
            if session._cancel_requested():
                raise TurnCancelled("the user stopped before form preparation")
            if budget is not None:
                budget.check()
        except TurnCancelled as exc:
            return "user_cancelled", str(exc)
        except BudgetExhausted as exc:
            return ("wall_clock_budget" if exc.kind == "wall_budget" else exc.kind), str(exc)
        return "", ""

    reason, detail = stopped()
    if reason:
        return current, reason, detail
    if current.render_state in {"pending", "blocked"}:
        return current, "agent_refused", "the current form page is not ready"
    reason, detail = verified(current)
    if reason:
        return current, reason, detail
    #: The error code the last dispatched command came back with, so the caller can
    #: tell "the page moved while we were aiming" from a real refusal.
    last_code = ""
    actions: list[tuple[str, dict, str]] = []
    if not observation_is_at_route(current, draft.route):
        actions.append(("goto", {"url": draft.route}, ""))

    def dispatch(action: str, args: dict, field: str,
                 before: PageObservation) -> tuple[PageObservation | None, str, str]:
        reason, detail = stopped()
        if reason:
            return before, reason, detail
        # Opening and filling the form is working the page, like any other operation --
        # except the typing into a form the application saves by itself, which that form's
        # own confirmation already covered, field by field.
        if action == "goto" or not draft.needs_confirmation:
            reason, detail = session._approvals.confirm_operation(
                step, before, action=action,
                url=args.get("url", "") if action == "goto" else "")
            if reason:
                return before, reason, detail
        # Permission may have waited. Recheck before dispatch, not only before asking.
        reason, detail = stopped()
        if reason:
            return before, reason, detail
        reason, detail = verified(before)
        if reason:
            return before, reason, detail
        prior_epoch = before.epoch
        nonlocal last_code
        last_code = ""
        try:
            session._browser_attempts += 1
            receipt = (session.browser.execute_with_budget(
                action, args, timeout_s=budget.check()) if budget is not None
                else session.browser.execute(action, args))
        except TurnCancelled as exc:
            return before, "user_cancelled", str(exc)
        except BudgetExhausted as exc:
            return before, ("wall_clock_budget" if exc.kind == "wall_budget" else exc.kind), str(exc)
        except TransportError as exc:
            return before, "transport_error", str(exc)
        except BusinessToolError as exc:
            session.ledger.append("form_dispatch_refused", step=step, tool=action,
                                  error_code=exc.code, dispatched=False)
            return before, ("permission_denied" if isinstance(exc, BusinessToolPermissionError)
                            else "agent_refused"), str(exc)
        if action not in ("observe", "wait_for"):
            session._approvals.settle_operation()
        if action not in ("observe", "handoff_form"):
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
        if receipt.error_code == "user_intervened":
            return receipt.observation or before, "user_cancelled", "the person took control"
        # Record the acknowledgement and write attribution before reporting a deadline.
        # A late receipt must never be discarded or invite replay of entered fields.
        reason, detail = stopped()
        if reason:
            return receipt.observation or before, reason, detail
        if (not receipt.ok or receipt.observation is None
                or receipt.observation.epoch <= prior_epoch):
            return (receipt.observation or before, "agent_refused",
                    f"{action} did not return a successful fresh page")
        after = receipt.observation
        if not observation_is_at_route(after, draft.route):
            return after, "agent_refused", "the application left the issued form route"
        if after.render_state == "blocked":
            return after, "agent_refused", "the form page became unavailable"
        if (action != "goto" and before.record_route
                and after.record_route != before.record_route):
            return after, "agent_refused", "the application changed the form record"
        reason, detail = verified(after)
        if reason:
            return after, reason, detail
        return after, "", ""

    for action, args, field in actions:
        current, reason, detail = dispatch(action, args, field, current)
        if reason:
            return current, reason, detail

    def matching(binding):
        return [element for element in current.elements if matches_form_control(
            element, field=binding.field, label=binding.label, control=binding.control,
            binding=binding.binding, scope=draft.control_scope)]

    def ready():
        nonlocal current
        for wait_ms in (100, 250, 500):
            if current.render_state != "pending":
                return "", ""
            current, reason, detail = dispatch("wait_for", {"ms": wait_ms}, "", current)
            if reason:
                return reason, detail
        return (("agent_refused", "the form page is still loading")
                if current.render_state == "pending" else ("", ""))

    for binding in draft.fields:
        reason, detail = ready()
        if reason:
            return current, reason, detail
        matches = matching(binding)
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
            matches = matching(binding)
        reason, detail = ready()
        if reason:
            return current, reason, detail
        matches = matching(binding)
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
                {"ref": matches[0].ref, "epoch": current.epoch, "value": binding.value,
                 **({"commit": True} if action == "fill" else {})},
                binding.field, current)
            if not reason:
                break
            if last_code != "stale_observation" or attempt == 2:
                return current, reason, detail
            current, reason, detail = dispatch("observe", {}, "", current)
            if reason:
                return current, reason, detail
            reason, detail = ready()
            if reason:
                return current, reason, detail
            matches = matching(binding)
            if len(matches) != 1:
                session.ledger.append(
                    "form_prefill_field_refused", step=step, field=binding.field,
                    matches=len(matches), dispatched=False, side_effect_possible=False)
                return current, "agent_refused", (
                    f"the current form did not expose one exact {binding.field} field")
    reason, detail = ready()
    if reason:
        return current, reason, detail
    submit_target = None
    if draft.submit_label:
        def submit_buttons():
            return [element for element in current.elements
                    if element.role == "button" and element.name == draft.submit_label
                    and element.attrs.get("form_scope") == draft.control_scope
                    and not form_control_unavailable(element.attrs)]

        buttons = submit_buttons()
        # Input focus can move the reviewed footer outside the viewport. Use the
        # existing generic scroll tool and fresh receipts; never re-enter a value or
        # click a guessed control. Look below, then above, under the same form verifier.
        for direction, pages in (("down", 1), ("up", 2)):
            if buttons:
                break
            current, reason, detail = dispatch(
                "scroll", {"direction": direction, "pages": pages}, "", current)
            if reason:
                return current, reason, detail
            reason, detail = ready()
            if reason:
                return current, reason, detail
            buttons = submit_buttons()
        if len(buttons) != 1:
            return current, "agent_refused", "the form has no unique enabled submit control"
        submit_target = {"ref": buttons[0].ref, "label": draft.submit_label,
                         "scope": draft.control_scope}
    handoff_fields = []
    for binding in draft.fields:
        matches = matching(binding)
        if len(matches) > 1 or (not matches and not draft.control_scope):
            return current, "agent_refused", (
                f"the current form no longer exposes one exact {binding.field} field")
        # An earlier exact fill may leave the viewport when the form scrolls. The
        # Runtime can recheck only its own entered DOM control in the same live
        # form instance; this is never a selector search or permission fallback.
        handoff_fields.append({**({"ref": matches[0].ref} if matches else {}), "field": binding.field,
                               "control": binding.control, "value": binding.value,
                               **({"binding": "label", "label": binding.label}
                                  if binding.binding == "label" else {}),
                               **({"scope": draft.control_scope}
                                  if draft.control_scope else {})})
    args = {"fields": handoff_fields}
    if submit_target is not None:
        args["submit"] = submit_target
    return dispatch("handoff_form", args, "", current)
