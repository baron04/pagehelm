"""Deterministic data calls return to the same Agent's ordinary read/evidence loop."""
from ..modeling.spend import BudgetExhausted
from ..transport.browser import TurnCancelled
from ..state.identity import PrincipalError
from .business_reads import execute_business_read
from .runstate import _Next


def data_tool(session, turn, call, name, args):
    def checkpoint():
        if session._cancel_requested():
            raise TurnCancelled("the person stopped this data read")
        if session._turn_budget is not None:
            session._turn_budget.check()

    # Check before creating a context: an expired turn cannot mint a new operation.
    try:
        checkpoint()
    except (TurnCancelled, BudgetExhausted) as exc:
        return stopped_read(session, turn, exc)
    # A catalogue or refused declaration is not a factual answer. Once the Agent
    # asks this data surface, completion requires typed evidence, even if none came back.
    session._answers.answer_mode = True
    try:
        context = session._approvals.context(turn.step, name)
    except PrincipalError:
        session.ledger.append("data_identity_refused", step=turn.step, tool=name, dispatched=False)
        return session._reply(turn, call, "DATA READ REFUSED: the operation identity is invalid or expired.")
    def read():
        return session.data_reads.execute(name, args, context=context,
            checkpoint=checkpoint, user_request=session._task_request_text)
    return execute_business_read(session, turn, call, name, args, context,
        context.audit_projection() if context is not None else {}, reader=read,
        counts_as_read=name != "inspect_data")


def stopped_read(session, turn, exc):
    reason = ("user_cancelled" if isinstance(exc, TurnCancelled) else
              "step_budget" if exc.kind == "step_budget" else
              "token_budget" if exc.kind == "token_budget" else "wall_clock_budget")
    return _Next(session._finish(turn.result, turn.started_at, reason, str(exc), steps=turn.step))
