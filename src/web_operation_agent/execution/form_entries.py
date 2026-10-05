"""Reviewed form opener tools over the existing page action path."""
import json

from ..business.errors import BusinessToolError, BusinessToolPermissionError
from .form_tools import _form_identity
from .page_actions import take_page_action
from .runstate import _Next


def _release(session, tools, entry, context, *, close=False):
    """Preserve a settled or exceptional outcome even if application cleanup fails."""
    for release in ([lambda: tools.discard(entry, context=context)]
                    + ([tools.close] if close else [])):
        try:
            release()
        except Exception as exc:
            session.ledger.append("form_entry_release_failed", error_type=type(exc).__name__)


def _refused(session, turn, call, name, exc, identity):
    if isinstance(exc, BusinessToolPermissionError):
        return _Next(session._permission_denied(turn.result, turn.started_at, turn.step,
            name, exc, dispatched=exc.layer == "application", identity=identity,
            event="application_form_permission_denied"))
    session.ledger.append("form_entry_refused", step=turn.step, tool=name,
                          error_code=exc.code, dispatched=False, side_effect_possible=False)
    return session._reply(turn, call, "FORM ENTRY UNAVAILABLE: " + str(exc)[:300])


def find_form_entries(session, turn, call, name, args):
    context, identity = _form_identity(session, turn.step, name)
    try:
        content = session._form_entry_tools.discover(turn.obs, args, context=context)
    except BusinessToolError as exc:
        return _refused(session, turn, call, name, exc, identity)
    session.ledger.append("form_entries_discovered", step=turn.step,
                          count=len(content["entries"]), side_effect_possible=False, identity=identity)
    return session._reply(turn, call, "CURRENT FORM ENTRIES:\n" + json.dumps(content, ensure_ascii=False))


def open_form_entry(session, turn, call, name, args):
    context, identity = _form_identity(session, turn.step, name)
    tools = session._form_entry_tools
    before = turn.obs
    try:
        entry = tools.plan(before, args, context=context)
    except BusinessToolError as exc:
        return _refused(session, turn, call, name, exc, identity)
    receipts = []
    try:
        outcome = take_page_action(session, turn, call, "click",
            {"ref": entry.control.ref, "epoch": before.epoch}, receipt_sink=receipts.append)
        if outcome.finished is not None or not receipts:
            _release(session, tools, entry, context)
            return outcome
        try:
            tools.acknowledged(entry, before, receipts[-1], context=context)
            # An opener receipt can precede the dialog's final render. Preserve it
            # for describe_form after another observation rather than binding early.
            feedback = "FORM ENTRY OPENED: observe/wait, then describe_form. Do not repeat the opener."
        except BusinessToolError as exc:
            feedback = "FORM ENTRY NOT VERIFIED: " + str(exc)[:300]
        session.ledger.append("form_entry_observed", step=turn.step, bound=False,
                              pending=tools.pending is not None, submitted=False, persisted=False,
                              identity=identity)
        turn.messages[-1]["content"] += "\n" + feedback
        return outcome
    except BaseException:
        _release(session, tools, entry, context, close=True)
        raise
