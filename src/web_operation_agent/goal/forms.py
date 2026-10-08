"""Bounded application-form handoff over the established fixed draft executor."""
from dataclasses import dataclass, replace
from types import SimpleNamespace

from ..business.errors import (BusinessToolError, BusinessToolIdentityError,
                               BusinessToolPermissionError, BusinessToolRequestError)
from ..business.forms import (ApplicationFormBindingPort, ApplicationFormOfferPort, ApplicationFormTools,
                              ApplicationFormEntryPort, ApplicationFormEntry, FormSubmitUnobserved)
from ..execution.form_tools import apply_form_draft
from ..modeling.provider import ProviderError
from ..state.identity import PrincipalError
from ..privacy import redact_sensitive_assignments, redact_session_handoffs
from .bind_fields import choose_labels
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
    error_code: str = ""
    #: Requested fields, by the names asked, that the open form shows no field for.
    unfilled: tuple[str, ...] = ()
    #: (field label, option asked) for the choices whose own list did not offer the option.
    unoffered: tuple[tuple[str, str], ...] = ()


def form_failure_message(reason: str, error_code: str, zh: bool) -> str:
    """User copy from typed outcomes only; exception prose stays in the audit."""
    causes = {
        "form_disabled": ("当前部署未启用这项表单操作", "this deployment has not enabled this form operation"),
        "form_plan_invalid": ("无法将请求绑定到一个明确的表单和填写目标", "the request could not bind to one exact form and set of fields"),
        "form_entry_unverified": ("无法核验目标表单已打开", "the opened form could not be verified"),
        "form_entry_unready": ("目标表单尚未加载完成", "the target form did not finish loading"),
        "form_entry_not_found": ("没有在页面上找到打开这张表单的入口", "no control that opens this form was found on the page"),
        "form_submit_unobserved": ("没有找到可核验的提交按钮，无法交回表单", "no verifiable submit control was found for handoff"),
        BusinessToolPermissionError.code: ("你的账号没有这项表单操作的权限", "your account lacks permission for this form operation"),
        BusinessToolIdentityError.code: ("无法核验当前登录身份，请重新登录后重试", "the current login could not be verified; sign in again"),
        "business_authentication": ("当前登录已失效，请重新登录", "the current login has expired; sign in again"),
        BusinessToolRequestError.code: ("目标表单或填写内容未通过应用校验，请核对表单和字段要求", "the target form or requested values did not pass application validation; check the form and field requirements"),
        "business_contract": ("应用返回的表单信息不完整或不一致", "the application's form information is incomplete or inconsistent"),
        "business_unavailable": ("表单读取服务暂不可用", "the form read service is unavailable"),
        "permission_denied": ("你的账号没有这项表单操作的权限", "your account lacks permission for this form operation"),
        "operation_not_confirmed": ("未获得你对这次操作的同意", "you did not approve this operation"),
        "approval_expired": ("这次操作的确认已过期", "approval for this operation expired"),
        "user_cancelled": ("操作已由你停止", "you stopped the operation"),
        "wall_budget": ("操作超过本轮时间上限", "the operation exceeded this turn's time limit"),
        "transport_error": ("无法取得可靠的浏览器执行回执", "a reliable browser action receipt was unavailable"),
    }
    pair = causes.get(reason) or causes.get(error_code) or (
        "页面状态或填写结果未通过核验，请检查当前表单",
        "the page state or entered values could not be verified; check the current form")
    return pair[0 if zh else 1]


class _FormBrowser:
    def __init__(self, executor, state):
        self.executor, self.state = executor, state
        self.input_dispatched = False
        self.error_code = ""

    def execute_with_budget(self, action, args, *, timeout_s):
        if action in self.executor.disabled_page_actions:
            self.error_code = "form_disabled"
            raise BusinessToolRequestError("the deployment disabled this form action")
        s = self.state
        s.step += 1
        s.writes.expect_decision(False)
        with s.receipt.stage("wait" if action == "wait_for" else "ui"):
            if action in ("fill", "select"):
                self.input_dispatched = True
            return s.browser.execute_with_budget(action, args, timeout_s=timeout_s)


def _shown_fields(executor, s, reference, values):
    """The planned values the open form shows a field for, and the names of the rest.

    A port that ties fields to the form's own labels says which requested fields it found
    no control for. Those are asked of the model once, against the labels nothing is tied
    to; what still has no control is left out and named, and the rest of the form is
    prepared as planned. Any other port is prepared as it always was.
    """
    port = executor.application.ports.forms
    if not isinstance(port, ApplicationFormBindingPort):
        return values, ()
    context = s.approvals.context(s.step, "bind_form")
    with s.receipt.stage("business"):
        missing, free = port.unbound_fields(reference, s.obs, context=context)
    if missing and free and s.chat is not None:
        wanted = dict(values)
        chosen = {}
        try:
            chosen = choose_labels(s.chat, run_id=s.run_id, labels=free, cancelled=s.cancelled,
                                   requested=tuple((name, wanted.get(key, "")) for key, name in missing),
                                   timeout_s=max(1.0, s.budget.remaining()))
        except (ValueError, ProviderError) as exc:
            s.ledger.append("goal_form_binding_failed", step=s.step, error_type=type(exc).__name__)
        bindings = tuple((key, chosen[name]) for key, name in missing if name in chosen)
        s.ledger.append("goal_form_fields_bound", step=s.step, asked=len(missing),
                        labels=len(free), bound=len(bindings))
        if bindings:
            with s.receipt.stage("business"):
                port.bind_fields(reference, bindings, context=context)
                missing, _ = port.unbound_fields(reference, s.obs, context=context)
    left = {key for key, _ in missing}
    return tuple(item for item in values if item[0] not in left), tuple(name for _, name in missing)


def _forms_shown(observation) -> frozenset:
    return frozenset(str(e.attrs.get("form_instance")) for e in observation.elements
                     if e.attrs.get("form_instance"))


def _found_entry(executor, s, port, plan, reference) -> str:
    """The planned form's reference once the page loop has opened it, or "".

    Every click the loop chooses is offered to the port first and pressed only if the
    port admits it; after each, the port is asked whether the planned form is what
    opened. A form that opened and is not the planned one ends the search: a dialog is
    in the way, and nothing was entered in it.
    """
    from .reach import EndReach, reach
    opened: list[str] = []

    def context(name):
        return s.approvals.context(s.step, name)

    def press(element):
        before = s.obs
        try:
            with s.receipt.stage("business"):
                entry = port.offer_entry(reference, before, element, context=context("form_entry"))
        except BusinessToolRequestError:
            s.ledger.append("goal_form_entry_offer_refused", step=s.step, role=element.role,
                            dispatched=False)
            return "refused: this control cannot be pressed for this"
        try:
            receipt = executor._dispatch(s, "click", {"ref": element.ref, "epoch": before.epoch})
            if receipt is None or not receipt.ok:
                return "it could not be pressed"
            appeared = False
            for attempt in range(4):
                executor._dispatch(s, "wait_for", {"ms": 400})
                if s.terminal_reason:
                    return "the turn ended"
                if s.obs.render_state == "pending":
                    continue
                try:
                    with s.receipt.stage("business"):
                        bound = port.accept_entry(entry, before, receipt, s.obs,
                                                  context=context("form_entry_receipt"))
                except BusinessToolRequestError:
                    # Nothing new that is a form: a menu opened, a tab switched. A form
                    # that did appear may still be loading the record it edits.
                    appeared = bool(_forms_shown(s.obs) - _forms_shown(before))
                    if not appeared:
                        return "pressed"
                    continue
                if isinstance(bound, str) and bound:
                    opened.append(bound)
                    return "pressed; the form opened"
            if appeared:
                raise EndReach("a form opened that is not the one asked for")
            return "pressed"
        finally:
            port.discard_entry(entry, context=context("form_entry_release"))

    title = plan.form.title or plan.label
    goal = (f'The form that edits the {title} record named "{plan.form.record}" is open.'
            if plan.form.record else f"The form that creates a new {title} is open.")
    result = reach(executor, s, goal=goal, steps=executor.page_reach_steps, press=press,
                   arrived=lambda observation: bool(opened), handoffs=executor._handoffs(),
                   names=(plan.form.record,) if plan.form.record else ())
    s.ledger.append("goal_form_entry_reached", step=s.step, arrived=bool(opened),
                    steps=result.steps, why=result.why[:200])
    return opened[0] if opened else ""


def handoff_form(executor, state, plan):
    """No selectors, submit actions or subsequent page operations; at most one model call,
    which chooses among the open form's own labels and enters nothing."""
    s = state
    if (not executor.config.allow_form_prefill
            or not {"fill", "handoffs"} <= capabilities_of(executor.goal_port)):
        return FormOutcome(reason="agent_refused", detail="form entry is not enabled", error_code="form_disabled")
    if plan.fill or plan.highlight:
        return FormOutcome(reason="agent_refused", detail="a form plan cannot mix legacy field actions", error_code="form_plan_invalid")
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
            entry = None
            try:
                with s.receipt.stage("business"):
                    entry = port.plan_entry(reference, before,
                                           context=s.approvals.context(s.step, "form_entry"))
            except BusinessToolRequestError:
                # The port names no opener here. Where the deployment lets the page loop
                # look for one, each control it presses is still the port's to admit and
                # the opened form still the port's to accept.
                if executor.page_reach_steps <= 0 or not isinstance(port, ApplicationFormOfferPort):
                    raise
            if entry is None:
                reference = _found_entry(executor, s, port, plan, reference)
                if not reference:
                    return FormOutcome(reason=s.terminal_reason or "agent_refused",
                                       detail="no control opened the planned form",
                                       error_code="form_entry_not_found")
            else:
                if (not isinstance(entry, ApplicationFormEntry) or entry.reference != reference
                        or entry.control.role != "button" or entry.control not in before.elements
                        or entry.control.epoch != before.epoch):
                    raise BusinessToolRequestError("the form entry is not an exact current control")
                try:
                    receipt = executor._dispatch(s, "click", {"ref": entry.control.ref, "epoch": before.epoch})
                    if receipt is None or not receipt.ok or receipt.navigated:
                        return FormOutcome(reason=s.terminal_reason or "agent_refused", detail="the form entry did not complete",
                                           error_code="form_entry_unverified")
                    if not executor._wait_ready(s, plan.expected_path, record=bool(before.record_route)):
                        return FormOutcome(reason=s.terminal_reason or "agent_refused", detail="the form entry is not ready",
                                           error_code="form_entry_unready")
                    with s.receipt.stage("business"):
                        reference = port.accept_entry(entry, before, receipt, s.obs,
                                            context=s.approvals.context(s.step, "form_entry_receipt"))
                finally:
                    port.discard_entry(entry, context=s.approvals.context(s.step, "form_entry_release"))
            if not isinstance(reference, str) or not reference:
                raise BusinessToolRequestError("the application did not bind the opened form")
        values, unfilled = _shown_fields(executor, s, reference, plan.form.values)
        if unfilled:
            s.ledger.append("goal_form_fields_unshown", step=s.step, count=len(unfilled),
                            kept=len(values))
        if not values:
            return FormOutcome(reason="agent_refused", detail="the form shows none of the planned fields",
                               error_code=BusinessToolRequestError.code, unfilled=unfilled)
        for attempt in range(4):
            try:
                with s.receipt.stage("business"):
                    draft, described = forms.prepare_current(s.obs, reference=reference,
                        values=values, context=s.approvals.context(s.step, "prepare_form"))
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
                                       detail="the form submit control could not be observed", error_code="form_submit_unobserved")
    except (BusinessToolError, PrincipalError) as exc:
        s.ledger.append("goal_form_preparation_refused", step=s.step,
                        error_code=(BusinessToolIdentityError.code if isinstance(exc, PrincipalError)
                                    else exc.code),
                        detail=redact_sensitive_assignments(redact_session_handoffs(str(exc)))[:200],
                        dispatched=False, submitted=False)
        return FormOutcome(reason=("permission_denied" if isinstance(exc, BusinessToolPermissionError)
                                   else "agent_refused"), detail=str(exc), error_code=(
            BusinessToolIdentityError.code if isinstance(exc, PrincipalError) else
            "form_submit_unobserved" if isinstance(exc, FormSubmitUnobserved) else exc.code))
    browser = _FormBrowser(executor, s)
    session = SimpleNamespace(_cancel_requested=s.cancelled, _browser_attempts=0,
        page_actions=0, _approvals=s.approvals, _writes=s.writes, ledger=s.ledger,
        browser=browser)
    unoffered: list = []
    final, reason, detail = apply_form_draft(session, draft, s.obs, s.step,
        budget=s.budget, unoffered=unoffered, verify=lambda obs: verification.check(draft, obs,
            context=s.approvals.context(s.step, "prefill_form")))
    if final is not None:
        s.obs = final
    s.page_actions += session.page_actions
    s.ledger.append("goal_form_handoff", step=s.step, complete=not reason,
        fields=len(draft.fields), submitted=False, persisted=False,
        terminal_reason=reason, detail=redact_sensitive_assignments(redact_session_handoffs(detail))[:200],
        browser_attempts=session._browser_attempts, input_dispatched=browser.input_dispatched,
        error_code=browser.error_code)
    skipped = {label for label, _ in unoffered}
    return FormOutcome(not reason, tuple(f for f in described["fields"] if f not in skipped),
                       draft.submit_label,
                       "wall_budget" if reason == "wall_clock_budget" else reason, detail,
                       browser.input_dispatched, browser.error_code, unfilled, tuple(unoffered))
