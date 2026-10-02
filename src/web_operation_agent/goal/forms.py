"""Bounded application-form handoff over the established fixed draft executor."""
from dataclasses import dataclass, replace
from types import SimpleNamespace

from ..business.errors import (BusinessToolError, BusinessToolIdentityError,
                               BusinessToolPermissionError, BusinessToolRequestError)
from ..business.forms import ApplicationFormTools
from ..execution.form_tools import apply_form_draft
from ..state.identity import PrincipalError
from .ports import FormPlan, capabilities_of


def combine_forms(plans):
    """One entry, one final handoff; conflicting or distinct targets cannot be combined."""
    first = plans[0]
    base = replace(first, form=None)
    values = {}
    for plan in plans:
        if replace(plan, form=None) != base or plan.form.reference != first.form.reference:
            raise BusinessToolRequestError("one turn can hand off only one exact form entry")
        for field, value in plan.form.values:
            if field in values and values[field] != value:
                raise BusinessToolRequestError("conflicting values name the same form field")
            values[field] = value
    if len(values) > 20:
        raise BusinessToolRequestError("a form handoff supports at most twenty fields")
    return replace(first, form=FormPlan(first.form.reference, tuple(values.items())))


@dataclass(frozen=True)
class FormOutcome:
    complete: bool = False
    fields: tuple[str, ...] = ()
    submit_label: str = ""
    reason: str = ""
    detail: str = ""


class _FormBrowser:
    def __init__(self, executor, state):
        self.executor, self.state = executor, state

    def execute_with_budget(self, action, args, *, timeout_s):
        if action in self.executor.disabled_page_actions:
            raise BusinessToolRequestError("the deployment disabled this form action")
        s = self.state
        s.step += 1
        s.writes.expect_decision(False)
        with s.receipt.stage("wait" if action == "wait_for" else "ui"):
            return s.browser.execute_with_budget(action, args, timeout_s=timeout_s)


def handoff_form(executor, state, plan):
    """No model calls, selectors, submit actions or subsequent page operations."""
    s = state
    if (not executor.config.allow_form_prefill
            or not {"fill", "handoffs"} <= capabilities_of(executor.goal_port)):
        return FormOutcome(reason="agent_refused", detail="form entry is not enabled")
    if plan.fill or plan.highlight:
        return FormOutcome(reason="agent_refused", detail="a form plan cannot mix legacy field actions")
    forms = ApplicationFormTools(executor.application.ports.forms,
                application_id=executor.application.descriptor.application_id)
    s.budget.check()
    try:
        with s.receipt.stage("business"):
            draft, described = forms.prepare_current(s.obs, reference=plan.form.reference,
                values=plan.form.values, context=s.approvals.context(s.step, "prepare_form"))
            verification = forms.take_verification(draft)
    except (BusinessToolError, PrincipalError) as exc:
        s.ledger.append("goal_form_preparation_refused", step=s.step,
                        error_code=(BusinessToolIdentityError.code if isinstance(exc, PrincipalError)
                                    else exc.code),
                        dispatched=False, submitted=False)
        return FormOutcome(reason=("permission_denied" if isinstance(exc, BusinessToolPermissionError)
                                   else "agent_refused"), detail=str(exc))
    session = SimpleNamespace(_cancel_requested=s.cancelled, _browser_attempts=0,
        page_actions=0, _approvals=s.approvals, _writes=s.writes, ledger=s.ledger,
        browser=_FormBrowser(executor, s))
    final, reason, detail = apply_form_draft(session, draft, s.obs, s.step,
        budget=s.budget, verify=lambda obs: verification.check(draft, obs,
            context=s.approvals.context(s.step, "prefill_form")))
    if final is not None:
        s.obs = final
    s.page_actions += session.page_actions
    s.ledger.append("goal_form_handoff", step=s.step, complete=not reason,
        fields=len(draft.fields), submitted=False, persisted=False,
        browser_attempts=session._browser_attempts)
    return FormOutcome(not reason, tuple(described["fields"]), draft.submit_label,
                       "wall_budget" if reason == "wall_clock_budget" else reason, detail)
