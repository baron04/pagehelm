"""Live label binding over application-reviewed transient editors.

The method fence protects the operation, not callbacks after handoff or expiry. It cannot
attest persistence or popup identity. Those remain the reviewed entry port's responsibility;
this measured adapter prototype only intersects its fields with the current controls and
the actor index's explicit aliases. Unknown editors never receive an opener or draft.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import re
from urllib.parse import urlsplit

from web_operation_agent.business.errors import BusinessToolRequestError
from web_operation_agent.business.forms import (ApplicationFormDescriptor, ApplicationFormEntry,
                                                 ApplicationFormValue)
from web_operation_agent.business.runtime import FormDraft
from web_operation_agent.schemas import PageObservation
from web_operation_agent.schemas.browser import FORM_INPUT_CONTROLS
from web_operation_agent.state.identity import OperationContext

from .page_guard import GuardRefused

_LABEL_NOISE = re.compile(r"[\s:：*＊]+")


def plain(text: object) -> str:
    """A visible label without spacing, colons or required marks, for comparison only."""
    return _LABEL_NOISE.sub("", str(text or "")).casefold()


def field_key(label: str) -> str:
    """The form-port reference of a requested label; never a selector."""
    return "f" + hashlib.sha256(plain(label).encode()).hexdigest()[:20]


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
    #: field key -> the exact requested value; types come only from the reviewed port.
    values: tuple[tuple[str, str], ...]
    openers: tuple[str, ...] = ()
    entity: str = ""
    route: str = ""
    record_id: str = ""


@dataclass
class _Bound:
    request: LiveRequest
    reference: str
    scope: str
    instance: str
    route: str
    mark: int
    turn: tuple[str, str]
    reviewed: ApplicationFormDescriptor
    described: ApplicationFormDescriptor | None = None
    #: Once values are issued the form is the live instance that was described: a field
    #: its own choices reveal or hide is the application's doing, and each issued field
    #: is still matched to exactly one control before it is entered or handed over.
    prepared: bool = False


class LiveFormPort:
    """ApplicationFormPort and ApplicationFormEntryPort over the currently open dialog."""

    required_scopes = frozenset({"yudao.read"})

    def __init__(self, evidence: FenceEvidence, reviewed):
        if not isinstance(evidence, FenceEvidence):
            raise TypeError("live forms require the deployment's fence evidence")
        if any(not callable(getattr(reviewed, name, None)) for name in (
                "plan_entry", "accept_entry", "discard_entry", "describe_form", "prepare_form")):
            raise TypeError("live forms require a reviewed transient entry port")
        self._evidence, self._reviewed = evidence, reviewed
        self._requests: dict[str, LiveRequest] = {}
        self._planned: tuple[LiveRequest, object, int, str, ApplicationFormEntry] | None = None
        self._bound: _Bound | None = None
        #: Requested labels the open form did not show, kept for the reply and the audit.
        self.unmatched: tuple[str, ...] = ()
        #: (requested label, visible label, tier) for each field the open form did show.
        self.matched: tuple[tuple[str, str, str], ...] = ()

    # -- what the goal port asks for --------------------------------------------------
    def expect(self, request: LiveRequest) -> None:
        """Remember one resolved handoff. A turn resolves several readings of a request;
        each keeps its own reference, and only the one the turn acts on is planned."""
        if (not isinstance(request, LiveRequest) or request.mode not in {"create", "update"}
                or not request.entity or not request.route.startswith("/")
                or bool(request.record_id) != (request.mode == "update")):
            raise ValueError("a live form request names a create or update editor")
        self._requests.pop(request.reference, None)
        self._requests[request.reference] = request
        while len(self._requests) > 16:
            self._requests.pop(next(iter(self._requests)))

    def click_policy(self, fallback=None):
        return LiveEntryClickPolicy(self, fallback)

    # -- the opener -------------------------------------------------------------------
    def entry_reference(self, request: LiveRequest) -> str:
        if request.mode == "update":
            if (request.entity == "crm/customer" and getattr(self._reviewed, "goal_updates", False)
                    and request.route == "/crm/customer/detail/" + request.record_id):
                return "customer-update:" + request.record_id
            return ""
        matches = [entry[0] for entry in getattr(self._reviewed, "goal_entries", ())
                   if entry[1] == request.entity and entry[2] == request.route]
        return matches[0] if len(matches) == 1 else ""

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
        parsed = urlsplit(observation.url)
        route = parsed.path + ("?" + parsed.query if parsed.query else "")
        reference = self.entry_reference(request)
        if not reference or route != request.route:
            raise BusinessToolRequestError("this target has no reviewed transient editor")
        reviewed_entry = self._reviewed.plan_entry(reference, observation, context=context)
        control = reviewed_entry.control
        # Taken before the opener is pressed, so a write the opener itself attempts counts.
        self._planned = (request, control, self._evidence.mark(), route, reviewed_entry)
        return ApplicationFormEntry(request.reference, control)

    def accept_entry(self, entry: ApplicationFormEntry, before: PageObservation, receipt,
                     observation: PageObservation, *, context: OperationContext) -> str:
        planned, self._planned = self._planned, None
        parsed = urlsplit(observation.url)
        route = parsed.path + ("?" + parsed.query if parsed.query else "")
        if (planned is None or entry.reference != planned[0].reference or entry.control != planned[1]
                or not receipt.ok or receipt.navigated or observation.render_state != "ready"
                or route != planned[3]
                or receipt.action != "click" or not receipt.write_guard_held
                or receipt.args_echo != {"ref": entry.control.ref, "epoch": before.epoch}
                or observation.epoch <= before.epoch):
            if planned is not None:
                self._reviewed.discard_entry(planned[4], context=context)
            raise BusinessToolRequestError("the live form opener did not complete on this page")
        try:
            reviewed_reference = self._reviewed.accept_entry(
                planned[4], before, receipt, observation, context=context)
        finally:
            self._reviewed.discard_entry(planned[4], context=context)
        reviewed = self._reviewed.describe_form(observation, context=context)
        if (reviewed is None or reviewed.persistence != "transient_until_submit"
                or reviewed.reference != reviewed_reference
                or reviewed.route != planned[0].route):
            raise BusinessToolRequestError("the editor identity or transient review could not be verified")
        earlier = {e.attrs.get("form_instance") for e in before.elements if e.attrs.get("form_instance")}
        opened = {(e.attrs.get("form_scope"), e.attrs.get("form_instance")) for e in observation.elements
                  if str(e.attrs.get("form_scope", "")).startswith("dialog")
                  and e.attrs.get("form_instance") and e.attrs.get("form_instance") not in earlier}
        if len(opened) != 1 or next(iter(opened))[0] != reviewed.control_scope:
            raise BusinessToolRequestError("the opener did not show exactly one new dialog form")
        scope, instance = next(iter(opened))
        parsed = urlsplit(observation.url)
        self._bound = _Bound(planned[0], f"{entry.reference}#{instance}"[:300], scope, instance,
                             parsed.path + ("?" + parsed.query if parsed.query else ""), planned[2],
                             (context.session_id, context.turn_id), reviewed)
        return self._bound.reference

    def discard_entry(self, entry: ApplicationFormEntry, *, context: OperationContext) -> None:
        if self._planned is not None:
            self._reviewed.discard_entry(self._planned[4], context=context)
        self._planned = None

    # -- the open form ----------------------------------------------------------------
    def _controls(self, bound: _Bound, observation: PageObservation):
        controls = [e for e in observation.elements if e.attrs.get("form_scope") == bound.scope]
        if not controls or any(e.attrs.get("form_instance") != bound.instance for e in controls):
            return None
        return controls

    def _match(self, bound: _Bound, labels: list[str]) -> dict[str, tuple[str, str]]:
        """(requested field key, tier) for each visible label one request names, uniquely."""
        candidates = {key: [label for label in labels if plain(label) in {plain(n) for n in names}]
                      for key, names in bound.request.fields}
        # Compute the whole relation before binding: two requested aliases for one field
        # must not silently overwrite it, and one alias for two fields remains ambiguous.
        names = dict(bound.request.fields)
        return {found[0]: (key, "exact" if plain(found[0]) == plain(names[key][0]) else "alias")
                for key, found in candidates.items() if len(found) == 1
                and sum(found[0] in others for others in candidates.values()) == 1}

    def _field(self, element, label: str, key: str):
        fields = [f for f in self._bound.reviewed.fields if plain(f.label) == plain(label)
                  and f.binding == "label" and element.role in f.controls]
        return replace(fields[0], reference=key, label=label) if len(fields) == 1 else None

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
        reviewed = self._reviewed.describe_form(observation, context=context)
        if reviewed != bound.reviewed:
            return None
        refused = self._evidence.since(bound.mark)
        if refused:
            raise BusinessToolRequestError(
                "the page fence refused a request during preparation; it was not filled further")
        if bound.prepared and bound.described is not None:
            submit = [e for e in controls if e.role == "button" and e.name == bound.described.submit_label]
            return bound.described if len(submit) == 1 else None
        inputs: dict[str, list] = {}
        for element in controls:
            label = element.attrs.get("field_label")
            if (element.role in FORM_INPUT_CONTROLS and isinstance(label, str) and label
                    and not element.attrs.get("field")):
                inputs.setdefault(label, []).append(element)
        unique = {label: items[0] for label, items in inputs.items() if len(items) == 1}
        assigned = self._match(bound, list(unique))
        fields = []
        for label, element in unique.items():
            key = assigned[label][0] if label in assigned else field_key("page:" + label)
            try:
                item = self._field(element, label, key)
            except ValueError:
                continue
            if item is not None:
                fields.append(item)
        submits = [e for e in controls if e.role == "button" and e.name == reviewed.submit_label]
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
            raise BusinessToolRequestError("the live form changed or its page fence refused a request")
        if (not isinstance(values, tuple) or not values
                or any(not isinstance(item, ApplicationFormValue) for item in values)):
            raise BusinessToolRequestError("live form values are empty or invalid")
        original = {f.reference: f for f in form.fields}
        translated = []
        for item in values:
            sources = [f for f in bound.reviewed.fields if plain(f.label) == plain(item.field.label)]
            if original.get(item.field.reference) != item.field or len(sources) != 1:
                raise BusinessToolRequestError("a value is not a current reviewed field")
            translated.append(replace(item, field=sources[0]))
        draft = self._reviewed.prepare_form(bound.reviewed, tuple(translated), context=context)
        mapped = {source.field.reference: live.field for source, live in zip(translated, values)}
        if any(field.field not in mapped for field in draft.fields):
            raise BusinessToolRequestError("the reviewed draft changed its fields")
        bound.prepared = True
        return replace(draft, fields=tuple(replace(field, field=mapped[field.field].reference,
                                                  label=mapped[field.field].label)
                                           for field in draft.fields))


class LiveEntryClickPolicy:
    """Only the opener just planned for this request, then whatever the deployment's
    navigation and tab policy already admits."""

    def __init__(self, forms: LiveFormPort, fallback=None):
        self._forms, self._fallback = forms, fallback

    def decide(self, observation, element):
        planned = self._forms._planned
        parsed = urlsplit(observation.url)
        route = parsed.path + ("?" + parsed.query if parsed.query else "")
        if (planned is not None and element == planned[1] and element.epoch == observation.epoch
                and observation.render_state == "ready" and route == planned[3]):
            return ""
        return (self._fallback.decide(observation, element) if self._fallback
                else "only the planned form opener is admitted here")


__all__ = ["FenceEvidence", "LiveEntryClickPolicy", "LiveFormPort", "LiveRequest", "field_key", "plain"]
