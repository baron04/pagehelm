"""Forms described from the page that is open, admitted by the method fence.

A measured prototype for one adapter. The reviewed form port knows a form from its source
and a per-entry review; this one knows it from what the current observation shows: the one
dialog that appeared after the planned opener was pressed, its labelled controls and its
submit control. Nothing here reads source. What makes filling such a form a draft rather
than a write is not a review of its component but the deployment's method fence: while the
assistant works the page every non-GET is refused, and a refusal seen between opening the
form and handing it over means the form tried to save, so the draft stops there.

The port fills only fields the request named, binds each to exactly one labelled control,
never presses the submit control and hands the highlighted form to the person.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import re
from urllib.parse import urlsplit

from web_operation_agent.business.errors import BusinessToolRequestError
from web_operation_agent.business.forms import (ApplicationFormDescriptor, ApplicationFormEntry,
                                                 ApplicationFormField, ApplicationFormValue,
                                                 FormChoice)
from web_operation_agent.business.runtime import FormDraft, FormDraftField
from web_operation_agent.business.tools.contracts import form_control_unavailable
from web_operation_agent.schemas import PageObservation
from web_operation_agent.schemas.browser import FORM_INPUT_CONTROLS
from web_operation_agent.state.identity import OperationContext

from .page_guard import GuardRefused

#: How this application's own pages label the controls that open an editor and the one
#: that submits it. Words, not pages: every generated list and dialog uses them.
CREATE_WORDS = ("新增", "新建", "创建", "添加")
UPDATE_WORDS = ("编辑", "修改")
SUBMIT_WORDS = frozenset({"确定", "保存", "提交"})
_LABEL_NOISE = re.compile(r"[\s:：*＊]+")
_GENERIC_PREFIX = ("所属", "所在", "请选择", "请输入")
_GENERIC_SUFFIX = ("名称", "姓名", "号码", "编号", "名", "号")
#: How a requested label was tied to a visible one, from strictest to loosest. Reported so
#: that a deployment can see how much rests on each.
MATCH_TIERS = ("exact", "stem", "core")
#: A label the server chose for a requested field among those nothing else matched.
CHOSEN = "chosen"
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?")


def plain(text: object) -> str:
    """A visible label without spacing, colons or required marks, for comparison only."""
    return _LABEL_NOISE.sub("", str(text or "")).casefold()


def field_key(label: str) -> str:
    """The form-port reference of a requested label; never a selector."""
    return "f" + hashlib.sha256(plain(label).encode()).hexdigest()[:20]


def _stem(label: str, entity_label: str) -> str:
    text = plain(label)
    owner = plain(entity_label)
    if owner and text.startswith(owner) and len(text) > len(owner):
        text = text[len(owner):]
    for prefix in _GENERIC_PREFIX:
        if text.startswith(prefix) and len(text) > len(prefix):
            text = text[len(prefix):]
    return text


def _core(label: str, entity_label: str) -> str:
    text = _stem(label, entity_label)
    for suffix in _GENERIC_SUFFIX:
        if text.endswith(suffix) and len(text) > len(suffix):
            return text[:-len(suffix)]
    return text


class FenceEvidence:
    """What the deployment's guard refused for one actor, as positions and paths."""

    def __init__(self, guard, actor):
        if not callable(actor) or not callable(getattr(guard, "refused_since", None)):
            raise TypeError("fence evidence needs the deployment guard and its actor")
        self._guard, self._actor = guard, actor

    def mark(self) -> int:
        try:
            return self._guard.refusal_mark(self._actor())
        except GuardRefused as exc:
            raise BusinessToolRequestError("the page fence cannot attest this actor") from exc

    def since(self, mark: int) -> tuple[tuple[str, str], ...]:
        try:
            return self._guard.refused_since(self._actor(), mark)
        except GuardRefused as exc:
            raise BusinessToolRequestError("the page fence evidence is incomplete") from exc


@dataclass(frozen=True)
class LiveRequest:
    """One handoff the goal port resolved: where its editor opens and what it fills."""
    reference: str
    mode: str
    entity_label: str
    #: field key -> every name the request or the index gives that field.
    fields: tuple[tuple[str, tuple[str, ...]], ...]
    #: field key -> the requested value, to tell a date entry from a choice.
    values: tuple[tuple[str, str], ...]
    openers: tuple[str, ...] = ()
    #: The record an update edits, by the name its own form shows.
    record: str = ""


@dataclass
class _Bound:
    request: LiveRequest
    reference: str
    scope: str
    instance: str
    route: str
    mark: int
    turn: tuple[str, str]
    described: ApplicationFormDescriptor | None = None
    #: Once values are issued the form is the live instance that was described: a field
    #: its own choices reveal or hide is the application's doing, and each issued field
    #: is still matched to exactly one control before it is entered or handed over.
    prepared: bool = False
    #: requested field key -> the visible label the server chose for it.
    chosen: dict[str, str] = field(default_factory=dict)
    #: What the last `unbound_fields` answer offered: field keys without a control, free labels.
    offered: tuple[frozenset, frozenset] = (frozenset(), frozenset())


class LiveFormPort:
    """ApplicationFormPort and ApplicationFormEntryPort over the currently open dialog."""

    required_scopes = frozenset({"yudao.read"})

    def __init__(self, evidence: FenceEvidence, *, hints=None):
        if not isinstance(evidence, FenceEvidence):
            raise TypeError("live forms require the deployment's fence evidence")
        self._evidence, self._hints = evidence, hints
        self._requests: dict[str, LiveRequest] = {}
        #: (request, the control to press, fence position before it, path, offered by the server)
        self._planned: tuple[LiveRequest, object, int, str, bool] | None = None
        self._bound: _Bound | None = None
        #: Requested labels the open form did not show, kept for the reply and the audit.
        self.unmatched: tuple[str, ...] = ()
        #: (requested label, visible label, tier) for each field the open form did show.
        self.matched: tuple[tuple[str, str, str], ...] = ()

    # -- what the goal port asks for --------------------------------------------------
    def expect(self, request: LiveRequest) -> None:
        """Remember one resolved handoff. A turn resolves several readings of a request;
        each keeps its own reference, and only the one the turn acts on is planned."""
        if not isinstance(request, LiveRequest) or request.mode not in {"create", "update"}:
            raise ValueError("a live form request names a create or update editor")
        self._requests.pop(request.reference, None)
        self._requests[request.reference] = request
        while len(self._requests) > 16:
            self._requests.pop(next(iter(self._requests)))

    def click_policy(self, fallback=None):
        return LiveEntryClickPolicy(self, fallback)

    # -- the opener -------------------------------------------------------------------
    def _opener(self, request: LiveRequest, observation: PageObservation):
        # A list's toolbar sits inside its query form; only a dialog's own buttons are not openers.
        buttons = [e for e in observation.elements
                   if e.role == "button" and not str(e.attrs.get("form_scope", "")).startswith("dialog")
                   and e.epoch == observation.epoch and not form_control_unavailable(e.attrs)]
        words = CREATE_WORDS if request.mode == "create" else UPDATE_WORDS
        owner = plain(request.entity_label)
        tiers = (frozenset(plain(name) for name in request.openers),
                 frozenset(word + owner for word in words) if owner else frozenset(),
                 frozenset(words))
        for names in tiers:
            matches = [e for e in buttons if plain(e.name) in names]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise BusinessToolRequestError("this page shows more than one matching form opener")
        raise BusinessToolRequestError("this page shows no form opener for the request")

    def plan_entry(self, reference: str, observation: PageObservation, *,
                   context: OperationContext) -> ApplicationFormEntry:
        self._planned = self._bound = None
        self.unmatched, self.matched = (), ()
        request = self._requests.get(reference)
        if request is None or context is None or observation.render_state != "ready":
            raise BusinessToolRequestError("the live form entry is absent or the page is not ready")
        context.require_live(application_id="yudao", required_scope="yudao.read")
        if any(str(e.attrs.get("form_scope", "")).startswith("dialog") for e in observation.elements):
            raise BusinessToolRequestError("another dialog is already open on this page")
        control = self._opener(request, observation)
        # Taken before the opener is pressed, so a write the opener itself attempts counts.
        self._planned = (request, control, self._evidence.mark(), urlsplit(observation.url).path, False)
        return ApplicationFormEntry(reference, control)

    def offer_entry(self, reference: str, observation: PageObservation, control, *,
                    context: OperationContext) -> ApplicationFormEntry:
        """A control the server found for a request this port named no opener for. It may
        be anything a person presses to get somewhere -- a row's button, a menu item --
        but never an input, and never the control that submits an open dialog."""
        self._planned = self._bound = None
        self.unmatched, self.matched = (), ()
        request = self._requests.get(reference)
        if request is None or context is None or observation.render_state != "ready":
            raise BusinessToolRequestError("the live form entry is absent or the page is not ready")
        context.require_live(application_id="yudao", required_scope="yudao.read")
        if (control not in observation.elements or control.epoch != observation.epoch
                or control.role in FORM_INPUT_CONTROLS or form_control_unavailable(control.attrs)
                or (str(control.attrs.get("form_scope", "")).startswith("dialog")
                    and plain(control.name) in SUBMIT_WORDS)):
            raise BusinessToolRequestError("this control cannot open the requested form")
        self._planned = (request, control, self._evidence.mark(), urlsplit(observation.url).path, True)
        return ApplicationFormEntry(reference, control)

    def accept_entry(self, entry: ApplicationFormEntry, before: PageObservation, receipt,
                     observation: PageObservation, *, context: OperationContext) -> str:
        planned = self._planned
        # An opener the server found may lead to a page that is the form (its own route)
        # rather than a dialog over the list; one this port named always opens a dialog.
        moved = receipt.navigated or (planned is not None
                                      and urlsplit(observation.url).path != planned[3])
        if (planned is None or entry.reference != planned[0].reference or entry.control != planned[1]
                or not receipt.ok or observation.render_state != "ready" or (moved and not planned[4])):
            if planned is not None and not planned[4]:
                self._planned = None
            raise BusinessToolRequestError("the live form opener did not complete on this page")
        earlier = {e.attrs.get("form_instance") for e in before.elements if e.attrs.get("form_instance")}
        opened = {(e.attrs.get("form_scope"), e.attrs.get("form_instance")) for e in observation.elements
                  if (moved or str(e.attrs.get("form_scope", "")).startswith("dialog"))
                  and e.attrs.get("form_scope") and e.attrs.get("field_label")
                  and e.attrs.get("form_instance") and e.attrs.get("form_instance") not in earlier}
        if len(opened) != 1:
            if not planned[4]:
                self._planned = None
            raise BusinessToolRequestError("the opener did not show exactly one new form")
        scope, instance = next(iter(opened))
        if planned[4] and planned[0].mode == "update" and planned[0].record:
            # One row's editor among many: the form must show the record that was meant.
            shown = {plain(e.attrs.get(key)) for e in observation.elements
                     if e.attrs.get("form_instance") == instance for key in ("value", "selected_option")}
            meant = plain(planned[0].record)
            # A name shared by several records is carried with what tells it apart
            # ("吴敏（采购）（某公司）"); the form shows the name itself.
            if not any(value and (value == meant or meant.startswith((value + "（", value + "(")))
                       for value in shown):
                raise BusinessToolRequestError("the editor that opened does not show the requested record")
        self._planned = None
        parsed = urlsplit(observation.url)
        self._bound = _Bound(planned[0], f"{entry.reference}#{instance}"[:300], scope, instance,
                             parsed.path + ("?" + parsed.query if parsed.query else ""), planned[2],
                             (context.session_id, context.turn_id))
        return self._bound.reference

    def discard_entry(self, entry: ApplicationFormEntry, *, context: OperationContext) -> None:
        self._planned = None

    def adopt_open(self, observation: PageObservation, fields, *, context: OperationContext):
        """The one dialog form open on the page, taken as it stands for the fields asked.
        Its fence position starts here: what it tried to save before is not this turn's."""
        if context is None or observation.render_state != "ready":
            raise BusinessToolRequestError("the page is not ready for its open form")
        context.require_live(application_id="yudao", required_scope="yudao.read")
        shown = {(e.attrs.get("form_scope"), e.attrs.get("form_instance")) for e in observation.elements
                 if str(e.attrs.get("form_scope", "")).startswith("dialog")
                 and e.attrs.get("field_label") and e.attrs.get("form_instance")}
        if (len(shown) != 1 or not isinstance(fields, tuple) or not fields
                or any(not isinstance(item, tuple) or len(item) != 2
                       or not all(isinstance(v, str) for v in item) or not item[0] for item in fields)
                or len({field_key(label) for label, _ in fields}) != len(fields)):
            raise BusinessToolRequestError("this page shows no single open form for these fields")
        scope, instance = next(iter(shown))
        digest = hashlib.sha256(repr(fields).encode()).hexdigest()[:16]
        reference = f"live:open:{instance}:{digest}"[:300]
        request = LiveRequest(reference, "update", "",
                              tuple((field_key(label), (label,)) for label, _ in fields),
                              tuple((field_key(label), value) for label, value in fields))
        self._planned, self.unmatched, self.matched = None, (), ()
        parsed = urlsplit(observation.url)
        self._bound = _Bound(request, reference, scope, instance,
                             parsed.path + ("?" + parsed.query if parsed.query else ""),
                             self._evidence.mark(), (context.session_id, context.turn_id))
        return reference, request.values

    # -- the open form ----------------------------------------------------------------
    def _controls(self, bound: _Bound, observation: PageObservation):
        controls = [e for e in observation.elements if e.attrs.get("form_scope") == bound.scope]
        if not controls or any(e.attrs.get("form_instance") != bound.instance for e in controls):
            return None
        return controls

    def _match(self, bound: _Bound, labels: list[str]) -> dict[str, tuple[str, str]]:
        """(requested field key, tier) for each visible label one request names, uniquely."""
        owner = bound.request.entity_label
        shapes = {"exact": lambda text: plain(text), "stem": lambda text: _stem(text, owner),
                  "core": lambda text: _core(text, owner)}
        assigned: dict[str, tuple[str, str]] = {}
        taken: set[str] = set()
        for key, label in bound.chosen.items():
            if label in labels and label not in assigned and key not in taken:
                assigned[label] = (key, CHOSEN)
                taken.add(key)
        for tier in MATCH_TIERS:
            shape = shapes[tier]
            for key, names in bound.request.fields:
                if key in taken:
                    continue
                wanted = {shape(name) for name in names}
                found = [label for label in labels if label not in assigned and shape(label) in wanted]
                if len(found) == 1:
                    assigned[found[0]] = (key, tier)
                    taken.add(key)
        return assigned

    def _field(self, element, label: str, key: str, value: str | None):
        hint = self._hints.field(self._bound.request, label, key) if self._hints else None
        if element.role == "spinbutton":
            return ApplicationFormField(key, label, kind="number", controls=("spinbutton",), binding="label")
        if element.role in {"combobox", "select", "radiogroup"}:
            choices = tuple(hint.choices) if hint is not None and hint.choices else ()
            if not choices and value is not None and element.role != "select":
                if element.role == "combobox" and (hint.kind in {"date", "datetime"} if hint is not None
                                                   else bool(_DATE.fullmatch(value) or _DATETIME.fullmatch(value))):
                    kind = "datetime" if (hint.kind == "datetime" if hint is not None
                                          else bool(_DATETIME.fullmatch(value))) else "date"
                    return ApplicationFormField(key, label, kind=kind, controls=("combobox",), binding="label")
                # The page does not list a closed dropdown's options. The one choice
                # declared is the label the request named; the Runtime selects it only if
                # the live listbox shows exactly that option, and verifies the selection.
                choices = (FormChoice(value, value),) if value.strip() else ()
            if not choices:
                return None
            return ApplicationFormField(key, label, kind="choice", controls=(element.role,),
                                        choices=choices, binding="label")
        kind = hint.kind if hint is not None and hint.kind in {"date", "datetime"} else "text"
        return ApplicationFormField(key, label, kind=kind, controls=("textbox",), binding="label")

    @staticmethod
    def _labelled(controls) -> dict:
        """label -> its one input, for the labels exactly one input of the form carries."""
        inputs: dict[str, list] = {}
        for element in controls:
            label = element.attrs.get("field_label")
            if (element.role in FORM_INPUT_CONTROLS and isinstance(label, str) and label
                    and not element.attrs.get("field")):
                inputs.setdefault(label, []).append(element)
        return {label: items[0] for label, items in inputs.items() if len(items) == 1}

    def _open(self, reference: str, observation: PageObservation, context):
        """The bound form and its controls while nothing has been issued for it yet."""
        bound = self._bound
        if (bound is None or bound.reference != reference or bound.prepared or context is None
                or observation.render_state != "ready"
                or (context.session_id, context.turn_id) != bound.turn):
            raise BusinessToolRequestError("the live form is absent or changed")
        context.require_live(application_id="yudao", required_scope="yudao.read")
        controls = self._controls(bound, observation)
        if controls is None:
            raise BusinessToolRequestError("the live form is absent or changed")
        return bound, controls

    def unbound_fields(self, reference: str, observation: PageObservation, *, context: OperationContext):
        bound, controls = self._open(reference, observation, context)
        unique = self._labelled(controls)
        assigned = self._match(bound, list(unique))
        found = {key for key, _ in assigned.values()}
        missing = tuple((key, names[0]) for key, names in bound.request.fields if key not in found)
        free = tuple((label, element.role) for label, element in unique.items() if label not in assigned)
        bound.offered = (frozenset(key for key, _ in missing), frozenset(label for label, _ in free))
        return missing, free

    def bind_fields(self, reference: str, bindings, *, context: OperationContext) -> None:
        bound = self._bound
        if (bound is None or bound.reference != reference or bound.prepared or context is None
                or (context.session_id, context.turn_id) != bound.turn):
            raise BusinessToolRequestError("the live form is absent or changed")
        keys, labels = bound.offered
        if (not isinstance(bindings, tuple) or not bindings
                or any(not isinstance(item, tuple) or len(item) != 2 for item in bindings)
                or len({key for key, _ in bindings}) != len(bindings)
                or len({label for _, label in bindings}) != len(bindings)
                or any(key not in keys or label not in labels for key, label in bindings)):
            raise BusinessToolRequestError("a form binding names a field or label that is not open")
        bound.chosen.update(bindings)
        bound.described = None

    def describe_form(self, observation: PageObservation, *,
                      context: OperationContext | None = None) -> ApplicationFormDescriptor | None:
        bound = self._bound
        if bound is None or context is None or observation.render_state != "ready":
            return None
        context.require_live(application_id="yudao", required_scope="yudao.read")
        if (context.session_id, context.turn_id) != bound.turn:
            return None
        controls = self._controls(bound, observation)
        if controls is None:
            return None
        refused = self._evidence.since(bound.mark)
        if refused:
            raise BusinessToolRequestError(
                "this form tried to save while it was being prepared; it was not filled further")
        if bound.prepared and bound.described is not None:
            submit = [e for e in controls if e.role == "button" and e.name == bound.described.submit_label]
            return bound.described if len(submit) == 1 else None
        unique = self._labelled(controls)
        assigned = self._match(bound, list(unique))
        values = dict(bound.request.values)
        fields = []
        for label, element in unique.items():
            key = assigned[label][0] if label in assigned else field_key("page:" + label)
            try:
                item = self._field(element, label, key, values.get(key) if label in assigned else None)
            except ValueError:
                continue
            if item is not None:
                fields.append(item)
        submits = [e for e in controls if e.role == "button" and plain(e.name) in SUBMIT_WORDS]
        if not fields or len(submits) != 1:
            return None
        names = dict((key, names[0]) for key, names in bound.request.fields)
        found = {key: (label, tier) for label, (key, tier) in assigned.items()}
        self.unmatched = tuple(names[key] for key in names if key not in found)
        self.matched = tuple((names[key], *found[key]) for key in names if key in found)
        form = ApplicationFormDescriptor(
            bound.reference, (bound.request.entity_label or "表单")[:200], bound.route, tuple(fields),
            scope=("new record" if bound.request.mode == "create" else "existing record"),
            control_scope=bound.scope, submit_label=submits[0].name)
        bound.described = form
        return form

    def prepare_form(self, form: ApplicationFormDescriptor,
                     values: tuple[ApplicationFormValue, ...], *,
                     context: OperationContext | None = None) -> FormDraft:
        bound = self._bound
        if (bound is None or context is None or bound.described != form
                or (context.session_id, context.turn_id) != bound.turn
                or self._evidence.since(bound.mark)):
            raise BusinessToolRequestError("the live form is absent, changed or tried to save")
        if (not isinstance(values, tuple) or not values
                or any(not isinstance(item, ApplicationFormValue) for item in values)):
            raise BusinessToolRequestError("live form values are empty or invalid")
        fields = []
        for item in values:
            value, option = item.value, ""
            if item.field.kind == "choice":
                option = next((c.label for c in item.field.choices if c.value == item.value), "")
                if not option:
                    raise BusinessToolRequestError("a live form choice is not one the form declared")
            elif item.field.kind == "datetime":
                try:
                    value = datetime.fromisoformat(value.strip()).isoformat(sep=" ", timespec="seconds")
                except ValueError:
                    raise BusinessToolRequestError(f"{item.field.label} requires a local date and time") from None
            elif item.field.kind in {"number", "date"}:
                value = value.strip()
            fields.append(FormDraftField(item.field.reference, item.field.label, value, item.control,
                                         binding="label", option_label=option))
        bound.prepared = True
        return FormDraft(form.route, tuple(fields), control_scope=form.control_scope,
                         submit_label=form.submit_label)


class LiveEntryClickPolicy:
    """Only the opener just planned for this request, then whatever the deployment's
    navigation and tab policy already admits."""

    def __init__(self, forms: LiveFormPort, fallback=None):
        self._forms, self._fallback = forms, fallback

    def decide(self, observation, element):
        planned = self._forms._planned
        if (planned is not None and element == planned[1] and element.epoch == observation.epoch
                and observation.render_state == "ready"
                and urlsplit(observation.url).path == planned[3]):
            return ""
        return (self._fallback.decide(observation, element) if self._fallback
                else "only the planned form opener is admitted here")


class FenceReachPolicy:
    """What the page loop may press: anything a person presses to get somewhere, never an
    input and never the control that submits an open dialog. What keeps such a press from
    writing is not this policy but the window it runs in: the application refuses its own
    writes there, and the deployment's fence refuses every request that is not a read."""

    def decide(self, observation, element):
        if element.role in FORM_INPUT_CONTROLS:
            return "an input is not pressed"
        if form_control_unavailable(element.attrs):
            return "the control is unavailable"
        if str(element.attrs.get("form_scope", "")).startswith("dialog") and plain(element.name) in SUBMIT_WORDS:
            return "that control submits the open form"
        return ""


__all__ = ["CREATE_WORDS", "FenceEvidence", "FenceReachPolicy", "LiveEntryClickPolicy", "LiveFormPort", "LiveRequest",
           "SUBMIT_WORDS", "UPDATE_WORDS", "field_key", "plain"]
