"""Application-issued form drafts and fixed generic browser form actions."""
from __future__ import annotations

import hashlib
import re

from ..transport.browser import TransportError, TurnCancelled
from ..business.runtime import FormDraft
from ..schemas import PageObservation
from .routes import observation_is_at_route
from .runstate import _Next, _Turn


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
