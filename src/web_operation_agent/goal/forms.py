"""Bounded application-form handoff over the established fixed draft executor."""
from dataclasses import dataclass, replace
from types import SimpleNamespace

from ..business.errors import (BusinessToolError, BusinessToolIdentityError,
                               BusinessToolPermissionError, BusinessToolRequestError)
from ..business.forms import ApplicationFormTools, ApplicationFormEntryPort, ApplicationFormEntry, FormSubmitUnobserved
from ..execution.form_tools import apply_form_draft
from ..state.identity import PrincipalError
from ..privacy import redact_sensitive_assignments, redact_session_handoffs
from .ports import capabilities_of


def combine_forms(plans):
    """One entry, one final handoff; conflicting or distinct targets cannot be combined."""
    first = plans[0]
    base = replace(first, form=None)
    values = {}
    for plan in plans:
        if (replace(plan, form=None) != base or plan.form.reference != first.form.reference
                or plan.form.open_entry != first.form.open_entry):
            raise BusinessToolRequestError("one turn can hand off only one exact form entry")
        for field, value in plan.form.values:
            if field in values and values[field] != value:
                raise BusinessToolRequestError("conflicting values name the same form field")
            values[field] = value
    if len(values) > 20:
        raise BusinessToolRequestError("a form handoff supports at most twenty fields")
    return replace(first, form=replace(first.form, values=tuple(values.items())))


@dataclass(frozen=True)
class FormOutcome:
    complete: bool = False
    fields: tuple[str, ...] = ()
    submit_label: str = ""
    reason: str = ""
    detail: str = ""
    input_dispatched: bool = False


class _FormBrowser:
    def __init__(self, executor, state):
        self.executor, self.state = executor, state
        self.input_dispatched = False

    def execute_with_budget(self, action, args, *, timeout_s):
        if action in self.executor.disabled_page_actions:
            raise BusinessToolRequestError("the deployment disabled this form action")
        s = self.state
        s.step += 1
        s.writes.expect_decision(False)
        with s.receipt.stage("wait" if action == "wait_for" else "ui"):
            if action in ("fill", "select"):
                self.input_dispatched = True
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
        reference = plan.form.reference
        if plan.form.open_entry:
            port = executor.application.ports.forms
            if not isinstance(port, ApplicationFormEntryPort):
                raise BusinessToolRequestError("this application does not expose reviewed form entries")
            before = s.obs
            with s.receipt.stage("business"):
                entry = port.plan_entry(reference, before,
                                       context=s.approvals.context(s.step, "form_entry"))
            if (not isinstance(entry, ApplicationFormEntry) or entry.reference != reference
                    or entry.control.role != "button" or entry.control not in before.elements
                    or entry.control.epoch != before.epoch):
                raise BusinessToolRequestError("the form entry is not an exact current control")
            try:
                receipt = executor._dispatch(s, "click", {"ref": entry.control.ref, "epoch": before.epoch})
                if receipt is None or not receipt.ok or receipt.navigated:
                    return FormOutcome(reason=s.terminal_reason or "agent_refused", detail="the form entry did not complete")
                if not executor._wait_ready(s, plan.expected_path, record=bool(before.record_route)):
                    return FormOutcome(reason=s.terminal_reason or "agent_refused", detail="the form entry is not ready")
                with s.receipt.stage("business"):
                    reference = port.accept_entry(entry, before, receipt, s.obs,
                                        context=s.approvals.context(s.step, "form_entry_receipt"))
            finally:
                port.discard_entry(entry, context=s.approvals.context(s.step, "form_entry_release"))
            if not isinstance(reference, str) or not reference:
                raise BusinessToolRequestError("the application did not bind the opened form")
        for attempt in range(4):
            try:
                with s.receipt.stage("business"):
                    draft, described = forms.prepare_current(s.obs, reference=reference,
                        values=plan.form.values, context=s.approvals.context(s.step, "prepare_form"))
                    verification = forms.take_verification(draft)
                break
            except FormSubmitUnobserved:
                if attempt == 3:
                    raise
                # Fixed read-only scroll, before preparing or entering any value.
                # The next descriptor must still verify the same application entry.
                receipt = executor._dispatch(s, "scroll", {"direction": "down", "pages": 1})
                if receipt is None or not receipt.ok or s.terminal_reason:
                    return FormOutcome(reason=s.terminal_reason or "agent_refused",
                                       detail="the form submit control could not be observed")
    except (BusinessToolError, PrincipalError) as exc:
        s.ledger.append("goal_form_preparation_refused", step=s.step,
                        error_code=(BusinessToolIdentityError.code if isinstance(exc, PrincipalError)
                                    else exc.code),
                        detail=redact_sensitive_assignments(redact_session_handoffs(str(exc)))[:200],
                        dispatched=False, submitted=False)
        return FormOutcome(reason=("permission_denied" if isinstance(exc, BusinessToolPermissionError)
                                   else "agent_refused"), detail=str(exc))
    browser = _FormBrowser(executor, s)
    session = SimpleNamespace(_cancel_requested=s.cancelled, _browser_attempts=0,
        page_actions=0, _approvals=s.approvals, _writes=s.writes, ledger=s.ledger,
        browser=browser)
    final, reason, detail = apply_form_draft(session, draft, s.obs, s.step,
        budget=s.budget, verify=lambda obs: verification.check(draft, obs,
            context=s.approvals.context(s.step, "prefill_form")))
    if final is not None:
        s.obs = final
    s.page_actions += session.page_actions
    s.ledger.append("goal_form_handoff", step=s.step, complete=not reason,
        fields=len(draft.fields), submitted=False, persisted=False,
        terminal_reason=reason, detail=redact_sensitive_assignments(redact_session_handoffs(detail))[:200],
        browser_attempts=session._browser_attempts, input_dispatched=browser.input_dispatched)
    return FormOutcome(not reason, tuple(described["fields"]), draft.submit_label,
                       "wall_budget" if reason == "wall_clock_budget" else reason, detail,
                       browser.input_dispatched)
