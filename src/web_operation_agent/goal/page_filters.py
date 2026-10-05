"""Bounded application-owned query controls using existing generic page actions.

Source is optional. The application must explicitly offer facets, issue this plan
under the current actor, revalidate it before each input, and attest the successful
applied read. Draft input values alone are never a completion postcondition.
"""
from dataclasses import dataclass
from urllib.parse import urlsplit

from ..business.errors import BusinessToolError, BusinessToolRequestError
from ..business.tools.contracts import form_control_unavailable
from ..state.identity import PrincipalError
from ..privacy import redact_sensitive_assignments, redact_session_handoffs


@dataclass(frozen=True)
class PageFilterField:
    label: str
    value: str
    control: str = "textbox"
    name: str = ""
    option_label: str = ""
    commit: bool = True

    def __post_init__(self):
        if (not isinstance(self.commit, bool)
                or any(not isinstance(v, str) for v in (self.control, self.label, self.value, self.name, self.option_label))
                or self.control not in {"textbox", "combobox", "select"}
                or not self.label or len(self.label) > 120 or len(self.value) > 4000
                or self.control == "select" and not self.option_label
                or self.control == "combobox" and not (self.option_label or self.name)):
            raise ValueError("invalid page filter field")


@dataclass(frozen=True)
class PageFilterDraft:
    route: str
    scope: str
    instance: str
    fields: tuple[PageFilterField, ...]
    button: str
    query_path: str
    parameters: tuple[tuple[str, str], ...]

    def __post_init__(self):
        if (any(not isinstance(v, str) or not v or len(v) > 500
                for v in (self.route, self.scope, self.instance, self.button, self.query_path))
                or not isinstance(self.fields, tuple) or any(not isinstance(f, PageFilterField) for f in self.fields)
                or not isinstance(self.parameters, tuple) or any(not isinstance(p, tuple) or len(p) != 2
                    or any(not isinstance(v, str) or len(v) > 120 for v in p) for p in self.parameters)
                or not self.route.startswith("/") or self.route.startswith("//")
                or not self.query_path.startswith("/") or self.query_path.startswith("//")
                or not self.scope or not self.instance or not self.button
                or not 1 <= len(self.fields) <= 12
                or len({(f.label, f.name) for f in self.fields}) != len(self.fields)
                or not 1 <= len(self.parameters) <= 12
                or len(dict(self.parameters)) != len(self.parameters)):
            raise ValueError("invalid application page filter draft")


def apply_page_filters(executor, state, plan):
    """No arbitrary actions, model calls, submissions or uncertain-input retries."""
    s = state
    port = executor.goal_port
    prepare = getattr(port, "prepare_page_filters", None)
    if not callable(prepare):
        return False
    dispatched = 0
    try:
        draft = prepare(plan, s.obs, context=s.approvals.context(s.step, "page_filters"))
        if not isinstance(draft, PageFilterDraft):
            raise BusinessToolRequestError("the application did not issue page filters")
        def current():
            s.budget.check()
            fresh = prepare(plan, s.obs, context=s.approvals.context(s.step, "page_filters_check"))
            if (fresh != draft or s.obs.render_state != "ready"
                    or urlsplit(s.obs.url).path != draft.route or s.obs.page_route != draft.route):
                raise BusinessToolRequestError("the page filter actor, entry or choices changed")
        def control(field=None):
            def labelled(e):
                if e.attrs.get("field_label") == field.label:
                    return True
                # A native range editor may label only its first input. Its second
                # endpoint must share one declared calendar popup with that labelled
                # input in the same live form, not merely be nearby in the DOM.
                popup = e.attrs.get("aria-controls")
                if (e.attrs.get("field_label") or not field.name or not popup
                        or e.attrs.get("aria-haspopup") != "dialog"):
                    return False
                peers = [p for p in s.obs.elements if p.role == "combobox"
                    and p.attrs.get("form_scope") == draft.scope
                    and p.attrs.get("form_instance") == draft.instance
                    and p.attrs.get("field_label") == field.label
                    and p.attrs.get("aria-controls") == popup
                    and p.attrs.get("aria-haspopup") == "dialog"]
                return len(peers) == 1
            candidates = [e for e in s.obs.elements if
                e.attrs.get("form_scope") == draft.scope
                and e.attrs.get("form_instance") == draft.instance
                and not form_control_unavailable(e.attrs)
                and (e.role == "button" and e.name == draft.button if field is None else
                     e.role == field.control and labelled(e)
                     and (not field.name or field.name in (e.name, e.attrs.get("placeholder"))))]
            if len(candidates) != 1:
                raise BusinessToolRequestError("a page query control is unavailable or ambiguous")
            return candidates[0]
        # Validate the entire plan before the first effect or approval.
        current()
        for field in draft.fields:
            control(field)
        control()
        for field in (*draft.fields, None):
            current()
            element = control(field)
            action = "click" if field is None else "select" if field.option_label else "fill"
            if action != "click" and action in getattr(executor, "disabled_page_actions", ()):
                raise BusinessToolRequestError("the deployment disabled this query input action")
            reason, detail = s.approvals.confirm_operation(s.step + 1, s.obs, action=action)
            if reason:
                s.terminal_reason, s.detail = reason, detail
                return False
            # Approval can wait; bind again before dispatch.
            current()
            element = control(field)
            args = {"ref": element.ref, "epoch": s.obs.epoch}
            if field is not None:
                args["value"] = field.value
                if action == "fill":
                    args["commit"] = field.commit
                else:
                    args["option_label"] = field.option_label
            s.step += 1
            s.writes.expect_decision(False)
            s.ledger.append("tool_call", step=s.step, tool=action, args=args)
            with s.receipt.stage("ui"):
                dispatched += 1
                receipt = s.browser.execute_with_budget(action, args, timeout_s=s.budget.check())
            s.approvals.settle_operation()
            outcome = s.writes.write_outcome(receipt, s.step, action_may_write=False)
            s.ledger.append("write_outcome", step=s.step, **{k:v for k,v in outcome.items() if k != "detail"})
            s.ledger.append("action_receipt", step=s.step, tool=action,
                            command_id=receipt.command_id, ok=receipt.ok, error_code=receipt.error_code)
            s.page_actions += 1
            if receipt.observation is not None:
                s.obs = receipt.observation
            reason, detail = s.writes.settle_write(outcome)
            if reason:
                s.terminal_reason, s.detail = reason, detail
                return False
            if not receipt.ok or receipt.navigated or not receipt.write_guard_held:
                return False
            if not executor._wait_ready(s, draft.route, record=False, requires_page_route=True):
                return False
        # This fact comes from a completed application GET, not the entered fields.
        expected = {"path": draft.query_path, "parameters": dict(draft.parameters)}
        complete = s.obs.page_query == expected
        s.ledger.append("goal_page_filters_settled", step=s.step, complete=complete,
                        fields=len(draft.fields), submitted=False)
        return complete
    except (BusinessToolError, PrincipalError) as exc:
        s.ledger.append("goal_page_filters_refused", step=s.step,
                        error_code=getattr(exc, "code", "identity"),
                        detail=redact_sensitive_assignments(redact_session_handoffs(str(exc)))[:160],
                        dispatched=bool(dispatched))
        return False
