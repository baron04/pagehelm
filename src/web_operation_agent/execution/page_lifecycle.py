"""Acquire the browser lifecycle only when a tool needs runtime page facts."""
from __future__ import annotations

from ..business.controlplane import ControlPlaneError
from ..schemas import BROWSER_ACTIONS, PageObservation
from ..transport.browser import TransportError
from ..modeling.spend import BudgetExhausted
from .context import _render_observation
from .runstate import _Next


PAGE_TOOLS = frozenset({
    "set_target", "open_source_route", "open_source_candidate", "open_skill_route",
    "enter_skill_app", "enter_application", "describe_form", "prepare_form",
    "prefill_form", "find_form_entries", "open_form_entry",
}) | BROWSER_ACTIONS


def requires_page(session, name: str) -> bool:
    if name in PAGE_TOOLS:
        return True
    tools = session.business_tools
    return bool(tools is not None and name in tools.tool_names and (
        tools.is_mutation(name)
        or (session.deterministic_read_navigation and not session.require_task_contract
            and session._navigation_only)))


def acquire_page(session, turn, call, name: str) -> _Next | None:
    """Bind fresh context before a page-dependent tool, never synthesising a page.

    A first call carrying guessed element references is not dispatched. Its result
    gives the same agent the real observation so it can make a grounded decision.
    """
    if turn.obs is not None:
        return None
    if session._cancel_requested():
        return _Next(session._finish(turn.result, turn.started_at, "user_cancelled",
                                    "the user stopped this task", steps=turn.step))
    try:
        if session._turn_budget is not None:
            session._turn_budget.check()
        session._writes.begin_control()
        obs = (session._page_entry() if session._page_entry is not None
               else session.browser.observe())
        if not isinstance(obs, PageObservation):
            raise TransportError("the browser did not supply a page observation")
        session.adopt_entry(obs)
        session._writes.start_observing()
    except BudgetExhausted as exc:
        reason = ("step_budget" if exc.kind == "step_budget" else
                  "token_budget" if exc.kind == "token_budget" else "wall_clock_budget")
        return _Next(session._finish(turn.result, turn.started_at, reason,
                                    str(exc), steps=turn.step))
    except (TransportError, ControlPlaneError) as exc:
        reason = ("user_cancelled" if session._cancel_requested() else
                  "control_plane_error" if isinstance(exc, ControlPlaneError)
                  else "transport_error")
        return _Next(session._finish(turn.result, turn.started_at, reason,
                                    str(exc), steps=turn.step))
    turn.obs = obs
    session.ledger.append("page_context_acquired", step=turn.step, tool=name,
                          page_epoch=obs.epoch, page_url=obs.url)
    if name == "observe":
        return None  # The ordinary observation tool owns its evidence options.
    session.ledger.append("page_context_before_dispatch", step=turn.step,
                          tool=name, dispatched=False, side_effect_possible=False)
    return session._reply(turn, call,
        "Page context acquired. The proposed tool was not dispatched; use the fresh "
        "observation to choose your next step.\nCURRENT PAGE:\n" + _render_observation(obs))
