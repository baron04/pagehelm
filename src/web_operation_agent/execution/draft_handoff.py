"""Final readback for a bounded explicit-submit draft procedure.

The application admits and verifies every entry. This helper checks the whole
observed draft once more, retains untouched fields, and uses the existing fixed
Runtime handoff for live DOM verification and highlighting. It never submits.
"""
from decimal import Decimal, InvalidOperation
import json

from .loop_guard import format_receipt_field_details
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
    if shown == value:
        return True
    return element.role == "combobox" and element.attrs.get("selected_option_path") == value


def add_draft_lines(context, button, count):
    """Execute bounded, application-reviewed additions, preserving the old draft.

    The application proves the line effect. Lifetime binds additions to one live
    form while its instance (the control set) is allowed to grow.
    """
    def refused(reason="readback"):
        audit = getattr(context, "audit", None)
        if callable(audit):
            audit("page_handoff_refused", reason=reason)
        return LoopToolResult("明细建行未通过核验，尚未填写或交接。", kind="page",
                              state="unverified", error_code="stale_binding")
    for _ in range(count):
        context.page.checkpoint()
        before = context.obs
        buttons = [e for e in before.elements if e.tag == "button" and e.role == "button"
                   and e.name.strip() == button.strip() and e.attrs.get("form_scope")
                   and e.attrs.get("form_lifetime")]
        if len(buttons) != 1:
            return refused()
        target = buttons[0]
        scope, lifetime = target.attrs["form_scope"], target.attrs["form_lifetime"]
        def snapshot(obs):
            fields = [e for e in draft_fields(obs) if e.attrs["form_scope"] == scope]
            if any(e.attrs.get("form_lifetime") != lifetime for e in fields):
                return None
            # Instance changes on addition; semantic identities and values must not.
            pairs = [(field_identity(e)[:1] + field_identity(e)[2:], field_value(e)) for e in fields]
            return dict(pairs) if len(dict(pairs)) == len(pairs) else None
        old = snapshot(before)
        if old is None:
            return refused()
        outcome = context.page.execute("click", {"ref": target.ref})
        if outcome.state != "completed":
            if outcome.error_code == "form_closed_or_readonly":
                return LoopToolResult("当前表单或弹窗已关闭或为只读状态，明细建行未执行。", kind="page",
                                      state=outcome.state, dispatch=outcome.dispatch,
                                      error_code="form_closed_or_readonly")
            return LoopToolResult("明细建行未完成，请核对当前观察。", kind="page", state=outcome.state,
                                  dispatch=outcome.dispatch, error_code=outcome.error_code)
        after, current = context.obs, snapshot(context.obs)
        anchors = [e for e in after.elements if e.attrs.get("form_scope") == scope
                   and e.attrs.get("form_lifetime") == lifetime]
        if (old is None or current is None or not anchors or before.url != after.url
                or before.page_route != after.page_route or not old.keys() < current.keys()
                or any(current[key] != value for key, value in old.items())):
            return refused()
    return None


def unrestorable_form(blocked, scope, instances=()):
    """The recorded unrestorable opening self-edit for this form, if any.

    `blocked` is the turn's unrestorable-form records (mapping or values); each record
    carries the form scope and whichever of instance/lifetime was observed. Scope plus
    any shared identity matches; when either side carries no identity, scope alone is
    enough -- refusing a mark is always the safe direction."""
    identities = ({instances} if isinstance(instances, str) else set(instances or ())) - {None, ""}
    records = blocked.values() if isinstance(blocked, dict) else (blocked or ())
    for record in records:
        if not isinstance(record, dict) or record.get("scope") != scope:
            continue
        known = {record.get("instance"), record.get("lifetime")} - {None, ""}
        if not known or not identities or known & identities:
            return record
    return None


def opening_changes_refusal(record, marked=""):
    """The answer every handoff path gives when it may not mark the submit control."""
    effects = "；".join(str(e) for e in (record.get("effects") or ())[:8])
    detail = effects or str(record.get("detail") or "")
    return LoopToolResult(
        ("不能标出提交按钮：这个表单打开时页面自动修改了字段且无法还原，"
         + (f"直接保存会把 {detail}；" if detail else "直接保存会带入未核对的变化；")
         + "请核对后自己提交。"),
        kind="page", state="refused", error_code="opening_changes")


def handoff_draft(context, before, expected, submit_label, *, blocked=None):
    """Expected pairs carry actual control identities, never model-given refs.

    Refuse if a field disappeared, was replaced, or any previously visible,
    untouched field changed. Offscreen/incomplete forms use ordinary observation
    and handoff instead; this combined procedure makes no completion claim.
    A form recorded in `blocked` (unrestorable opening self-edits) is never marked:
    the refusal says what saving it directly would change.
    """
    def refused(reason="readback", **detail):
        audit = getattr(context, "audit", None)
        if callable(audit):
            audit("page_handoff_refused", reason=reason, **detail)
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
    lifetimes = {e.attrs.get("form_lifetime") for e in initial
                 if (e.attrs.get("form_scope"), e.attrs.get("form_instance")) == (scope, instance)}
    blocked_form = unrestorable_form(blocked, scope, {instance} | lifetimes)
    if blocked_form is not None:
        return opening_changes_refusal(blocked_form)
    old = [e for e in initial if (e.attrs["form_scope"], e.attrs["form_instance"]) == (scope, instance)]
    now = [e for e in current if (e.attrs["form_scope"], e.attrs["form_instance"]) == (scope, instance)]
    def comparison_map(fields):
        # DOM ids distinguish only untouched anonymous controls for before/after
        # comparison. They never become a field binding or enter the Runtime command.
        ids = {}
        for element in fields:
            identifier = element.attrs.get("id")
            if isinstance(identifier, str) and identifier.strip():
                group = (element.role, element.tag, identifier)
                ids[group] = ids.get(group, 0) + 1
        mapped = {}
        for element in fields:
            key = field_identity(element)
            if not any(key[3:]):
                identifier = element.attrs.get("id")
                if (key in wanted or not isinstance(identifier, str) or not identifier.strip()
                        or ids.get((element.role, element.tag, identifier)) != 1):
                    return None
                key = (*key, "untouched_dom_id", element.tag, identifier)
            if key in mapped:
                return None
            mapped[key] = element
        return mapped

    old_map, now_map = comparison_map(old), comparison_map(now)
    if (old_map is None or now_map is None
            or old_map.keys() != now_map.keys() or not wanted.keys() <= now_map.keys()
            or any(not value_matches(now_map[key], value) for key, value in wanted.items())
            or any(field_value(now_map[key]) != field_value(element)
                   for key, element in old_map.items() if key not in wanted)):
        return refused()
    plain_submit = "".join((submit_label or "").split())
    buttons = [e for e in after.elements if e.role == "button" and "".join((e.name or "").split()) == plain_submit
               and e.attrs.get("form_scope") == scope and e.attrs.get("form_instance") == instance]
    scrolled, scrolled_map = False, None
    if not buttons and scope.startswith("dialog"):
        # A tall modal can show every requested field while its footer is below
        # the viewport. Read back the whole draft first, then bring the footer
        # into view. The Runtime will recheck the entered fields against their
        # live DOM controls before it highlights the submit button.
        if any(not (now_map[key].attrs.get("field") or now_map[key].attrs.get("field_label"))
               for key in wanted):
            return refused()
        anchor = next(iter(now_map.values()), None)
        if anchor is None:
            return refused()
        moved = context.page.execute("scroll", {"direction": "down", "pages": 1, "ref": anchor.ref})
        if moved.state != "completed" or context.terminal_reason:
            return refused()
        scrolled = True
        at_submit = context.obs
        if at_submit.url != after.url or at_submit.page_route != after.page_route:
            return refused()
        # Controls lower in the dialog first appear here; the handoff itself
        # must leave this view unchanged.
        scrolled_map = comparison_map([e for e in draft_fields(at_submit)
            if (e.attrs["form_scope"], e.attrs["form_instance"]) == (scope, instance)])
        if scrolled_map is None:
            return refused("scrolled_ambiguous")
        buttons = [e for e in at_submit.elements if e.role == "button"
                   and "".join((e.name or "").split()) == plain_submit
                   and e.attrs.get("form_scope") == scope
                   and e.attrs.get("form_instance") == instance]
    if len(buttons) != 1:
        return refused()
    fields = []
    for key, value in wanted.items():
        element = now_map[key]
        a = element.attrs
        field = a.get("field", "")
        label = a.get("field_label", "")
        title = a.get("title", "")
        if not field and not label and not title:
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
        fields.append({**({} if scrolled else {"ref": element.ref}),
                       "field": field or label or title, "control": element.role,
                       "value": native, "scope": scope,
                       **({"binding": "label", "label": label} if not field and label else
                          {"binding": "ref", "instance": instance} if not field else {}),
                       **({"option_label": option} if option is not None else {})})
    outcome = context.page.handoff(fields, {"ref": buttons[0].ref, "label": buttons[0].name, "scope": scope})
    if outcome.state != "completed" or outcome.receipt is None:
        if outcome.error_code == "form_closed_or_readonly":
            return LoopToolResult("当前表单或弹窗已关闭或为只读状态，未完成交接。", kind="page",
                                  state=outcome.state, dispatch=outcome.dispatch,
                                  error_code="form_closed_or_readonly")
        extra = format_receipt_field_details(outcome.receipt)
        msg = "表单交接未完成，请核对当前观察。" + (f"\n{extra}" if extra else "")
        return LoopToolResult(msg, kind="page", state=outcome.state,
                              dispatch=outcome.dispatch, error_code=outcome.error_code)
    final = comparison_map([e for e in draft_fields(context.obs)
             if (e.attrs["form_scope"], e.attrs["form_instance"]) == (scope, instance)])
    if final is None:
        return refused("final_ambiguous")
    baseline = scrolled_map if scrolled else now_map
    final_untouched = {key for key in final if len(key) > 6}
    now_untouched = {key for key in baseline if len(key) > 6}
    if final_untouched != now_untouched:
        return refused("final_untouched_set", scrolled=scrolled,
                       added=[list(map(str, key[-2:])) for key in final_untouched - now_untouched][:8],
                       missing=[list(map(str, key[-2:])) for key in now_untouched - final_untouched][:8])
    if final.keys() != baseline.keys():
        return refused("final_field_set")
    if any(key in final and not value_matches(final[key], value) for key, value in wanted.items()):
        return refused("final_requested_value")
    if any(key in final and field_value(final[key]) != field_value(e)
           for key, e in {**baseline, **old_map}.items() if key not in wanted):
        return refused("final_untouched_value")
    context.audit("page_handoff_ready", observation_epoch=context.obs.epoch, submitted=False, persisted=False)
    extra = format_receipt_field_details(outcome.receipt)
    msg = "表单已核验，提交按钮已标出。" + (f"\n{extra}" if extra else "")
    return LoopToolResult(msg, kind="page", dispatch="acknowledged",
                          marked=buttons[0].name[:200], completion_command=outcome.receipt.command_id)
