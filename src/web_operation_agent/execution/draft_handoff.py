"""Final readback for a bounded explicit-submit draft procedure.

The application admits and verifies every entry. This helper checks the whole
observed draft once more, retains untouched fields, and uses the existing fixed
Runtime handoff for live DOM verification and highlighting. It never submits.
"""
from decimal import Decimal, InvalidOperation
import json

from .model_loop_contracts import LoopToolResult


def field_identity(element):
    a = element.attrs
    return (a.get("form_scope", ""), a.get("form_instance", ""), element.role,
            a.get("field", ""), a.get("field_label", ""), a.get("title", ""))


def draft_fields(observation):
    return [e for e in observation.elements if e.role in {
        "textbox", "combobox", "select", "spinbutton", "radiogroup"}
        and e.attrs.get("form_scope") and e.attrs.get("form_instance")]


def field_value(element):
    return element.attrs.get("selected_option", element.attrs.get("value", ""))


def value_matches(element, value):
    shown = field_value(element)
    if element.role == "spinbutton":
        try:
            left, right = Decimal(shown), Decimal(value)
            return left.is_finite() and right.is_finite() and left == right
        except InvalidOperation:
            return False
    return shown == value


def handoff_draft(context, before, expected, submit_label):
    """Expected pairs carry actual control identities, never model-given refs.

    Refuse if a field disappeared, was replaced, or any previously visible,
    untouched field changed. Offscreen/incomplete forms use ordinary observation
    and handoff instead; this combined procedure makes no completion claim.
    """
    def refused():
        return LoopToolResult("表单最终回读未通过，尚未交接；请核对当前表单。", kind="page",
                              state="unverified", error_code="unverified")

    context.page.checkpoint()
    after = context.obs
    initial, current = draft_fields(before), draft_fields(after)
    if not expected or before.url != after.url or before.page_route != after.page_route:
        return refused()
    wanted = dict(expected)
    if len(wanted) != len(expected):
        return refused()
    scopes = {(key[0], key[1]) for key in wanted}
    if len(scopes) != 1:
        return refused()
    scope, instance = next(iter(scopes))
    old = [e for e in initial if (e.attrs["form_scope"], e.attrs["form_instance"]) == (scope, instance)]
    now = [e for e in current if (e.attrs["form_scope"], e.attrs["form_instance"]) == (scope, instance)]
    old_map, now_map = {field_identity(e): e for e in old}, {field_identity(e): e for e in now}
    if (len(old_map) != len(old) or len(now_map) != len(now)
            or old_map.keys() != now_map.keys() or not wanted.keys() <= now_map.keys()
            or any(not value_matches(now_map[key], value) for key, value in wanted.items())
            or any(field_value(now_map[key]) != field_value(element)
                   for key, element in old_map.items() if key not in wanted)):
        return refused()
    buttons = [e for e in after.elements if e.role == "button" and e.name.strip() == submit_label.strip()
               and e.attrs.get("form_scope") == scope and e.attrs.get("form_instance") == instance]
    if len(buttons) != 1:
        return refused()
    fields = []
    for key, value in wanted.items():
        element = now_map[key]
        a = element.attrs
        field = a.get("field", "")
        label = a.get("field_label", "")
        if not field and not label:
            return refused()
        native, option = value, None
        if element.role == "radiogroup":
            try:
                choices = json.loads(a.get("radio_options", "[]"))
            except ValueError:
                return refused()
            matches = [c for c in choices if isinstance(c, dict) and c.get("label") == value]
            if len(matches) != 1 or not isinstance(matches[0].get("value"), str):
                return refused()
            native, option = matches[0]["value"], value
        elif a.get("selected_option") is not None:
            option = value
        fields.append({"ref": element.ref, "field": field or label, "control": element.role,
                       "value": native, "scope": scope,
                       **({"binding": "label", "label": label} if not field else {}),
                       **({"option_label": option} if option is not None else {})})
    outcome = context.page.handoff(fields, {"ref": buttons[0].ref, "label": buttons[0].name, "scope": scope})
    if outcome.state != "completed" or outcome.receipt is None:
        return LoopToolResult("表单交接未完成，请核对当前观察。", kind="page", state=outcome.state,
                              dispatch=outcome.dispatch, error_code=outcome.error_code)
    final = {field_identity(e): e for e in draft_fields(context.obs)
             if (e.attrs["form_scope"], e.attrs["form_instance"]) == (scope, instance)}
    if (final.keys() != now_map.keys()
            or any(not value_matches(final[key], value) for key, value in wanted.items())
            or any(field_value(final[key]) != field_value(e) for key, e in old_map.items() if key not in wanted)):
        return refused()
    context.audit("page_handoff_ready", observation_epoch=context.obs.epoch, submitted=False, persisted=False)
    return LoopToolResult("表单已核验，提交按钮已标出。", kind="page", dispatch="acknowledged",
                          marked=buttons[0].name[:200], completion_command=outcome.receipt.command_id)
