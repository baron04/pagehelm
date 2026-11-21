"""Declarative form, prefill, draft-step, and read-action admission ports."""
from __future__ import annotations

import hashlib
import json
from typing import Callable
from urllib.parse import urljoin, urlsplit

from web_operation_agent.business.draft_steps import DraftStepPermit
from web_operation_agent.business.errors import BusinessToolError, BusinessToolRequestError
from web_operation_agent.business.form_entries import FormEntryCandidate
from web_operation_agent.business.forms import (
    ApplicationFormDescriptor,
    ApplicationFormEntry,
    ApplicationFormField,
    ApplicationFormValue,
)
from web_operation_agent.business.prefill_conversion import MutationPrefillOffer
from web_operation_agent.business.tools.contracts import FormDraft, FormDraftField
from web_operation_agent.execution.action_admission import (
    ActionAdmissionRefused,
    ActionEffect,
    ActionPermit,
    ActionResultPending,
)
from web_operation_agent.execution.draft_handoff import draft_fields, field_identity, field_value, handoff_draft
from web_operation_agent.execution.model_loop_contracts import LoopToolResult
from web_operation_agent.state.identity import PrincipalError
from .openapi import FormSpec, PageSpec


def _route_matches(route: str, pattern: str) -> bool:
    if pattern.endswith("*"):
        return route.startswith(pattern[:-1])
    return route.startswith(pattern) if pattern.endswith("/") else route == pattern


class DeclarativeFormPort:
    """Declarative implementation of ``ApplicationFormPort`` and form entry hooks."""

    def __init__(self, forms: tuple[FormSpec, ...], *, executor=None, required_scopes: frozenset[str] = frozenset()):
        self._forms = tuple(forms)
        self._by_route = {form.route: form for form in self._forms}
        self._by_entry = {form.entry_reference: form for form in self._forms if form.entry_reference}
        self._executor = executor
        scopes = set(required_scopes) | {f.required_scope for f in self._forms if f.required_scope}
        self.required_scopes = frozenset(scopes)
        self._planned = None

    def form_entries(self, observation, *, context=None):
        if observation.render_state != "ready":
            return ()
        path = urlsplit(observation.url).path
        out = []
        for form in self._forms:
            if form.entry_route and form.entry_reference and form.entry_label and observation.page_route == form.entry_route and path == form.entry_route:
                controls = [e for e in observation.elements if e.role == "link" and e.name == form.entry_label and e.attrs.get("href") == form.route]
                if len(controls) == 1:
                    out.append(FormEntryCandidate(form.entry_reference, form.entry_label, form.entry_action))
        return tuple(out)

    def plan_entry(self, reference: str, observation, *, context):
        self._planned = None
        form = self._by_entry.get(reference)
        if form is None or not any(c.reference == reference for c in self.form_entries(observation, context=context)):
            raise BusinessToolRequestError("the requested form entry is not current")
        control = next(e for e in observation.elements if e.role == "link" and e.name == form.entry_label and e.attrs.get("href") == form.route)
        entry = ApplicationFormEntry(reference, control)
        self._planned = (entry, context.principal.binding_digest, context.session_id, context.turn_id, form)
        return entry

    def accept_entry(self, entry, before, receipt, observation, *, context):
        planned, self._planned = self._planned, None
        if planned is None:
            raise BusinessToolRequestError("the form entry receipt was not verified")
        expected_entry, digest, session_id, turn_id, form = planned
        if (
            (expected_entry, digest, session_id, turn_id) != (entry, context.principal.binding_digest, context.session_id, context.turn_id)
            or not receipt.ok or receipt.action != "click" or receipt.args_echo != {"ref": entry.control.ref, "epoch": before.epoch}
            or observation.epoch <= before.epoch or observation.render_state != "ready"
            or observation.page_route != form.route or urlsplit(observation.url).path != form.route
        ):
            raise BusinessToolRequestError("the form entry receipt was not verified")
        return form.reference

    def discard_entry(self, entry, *, context):
        if self._planned is not None and self._planned[0] == entry:
            self._planned = None

    def decide(self, observation, element):
        if self._planned is not None and self._planned[0].control == element and element in observation.elements and element.epoch == observation.epoch:
            return ""
        return "only a current reviewed form opener may be clicked"

    def describe_form(self, observation, *, context=None) -> ApplicationFormDescriptor | None:
        form = self._by_route.get(urlsplit(observation.url).path)
        if form is None:
            return None
        return ApplicationFormDescriptor(
            reference=form.reference, title=form.title, route=form.route,
            fields=tuple(
                ApplicationFormField(f.name, f.label, kind=f.form_kind, controls=f.controls, required=f.required, choices=f.choices, binding=f.binding)
                for f in form.fields if f.in_form
            ),
            scope=form.scope_description, control_scope=form.control_scope, submit_label=form.submit_label,
        )

    def prepare_form(self, form: ApplicationFormDescriptor, values: tuple[ApplicationFormValue, ...], *, context=None) -> FormDraft:
        spec = next((f for f in self._forms if f.reference == form.reference or f.route == form.route), None)
        raw = {item.field.reference: item.value for item in values}
        normalized = dict(raw)
        if spec is not None and spec.normalizers:
            try:
                for key, fn in spec.normalizers.items():
                    if key in raw:
                        normalized[key] = fn(raw[key])
            except BusinessToolError as exc:
                raise BusinessToolRequestError(str(exc)) from exc
        return FormDraft(
            route=form.route,
            fields=tuple(
                FormDraftField(
                    item.field.reference, item.field.label, normalized[item.field.reference],
                    item.control, binding=item.field.binding,
                    option_label=next((c.label for c in item.field.choices if c.value == item.value), ""),
                )
                for item in values
            ),
            control_scope=form.control_scope, submit_label=form.submit_label,
        )


class DeclarativeMutationPrefill:
    """Convert reviewed write proposals into transient, explicit-save form handoffs."""

    def __init__(self, form_port: DeclarativeFormPort, forms: tuple[FormSpec, ...], *, application_id: str, required_scope: str):
        self.forms, self.application_id, self.required_scope = form_port, application_id, required_scope
        self._specs = {form.prefill_from_tool: form for form in forms if form.prefill_from_tool}
        self._by_target = {form.route: form for form in forms}

    @classmethod
    def from_adapter(cls, adapter) -> "DeclarativeMutationPrefill":
        return cls(adapter.ports.forms, tuple(adapter.spec.forms), application_id=adapter.spec.application_id, required_scope=adapter.write_scope)

    def offer(self, proposal, *, context, observation) -> MutationPrefillOffer | None:
        spec = self._specs.get(proposal.tool)
        if spec is None:
            return None
        context.require_live(application_id=self.application_id, required_scope=self.required_scope)
        entries = self.forms.form_entries(observation, context=context)
        if observation.render_state != "ready" or observation.page_route != spec.entry_route or urlsplit(observation.url).path != spec.entry_route or len(entries) != 1 or entries[0].reference != spec.entry_reference:
            return None
        required_fields = [f.name for f in spec.fields if f.required]
        if not required_fields or set(proposal.args) != set(required_fields):
            raise BusinessToolRequestError("mutation proposal has no exact form fields")
        pairs = []
        for key in required_fields:
            val = proposal.args.get(key)
            if not isinstance(val, str):
                raise BusinessToolRequestError("mutation proposal has no exact form fields")
            pairs.append((key, val))
        return MutationPrefillOffer(proposal.content_digest, context.binding_digest, spec.route, tuple(pairs))

    @staticmethod
    def _refused() -> LoopToolResult:
        return LoopToolResult("The selected form could not be verified.", kind="page", state="unverified", error_code="unverified")

    def handoff(self, offer: MutationPrefillOffer, proposal, *, context) -> LoopToolResult:
        offer.require(proposal, context.operation)
        context.operation.require_live(application_id=self.application_id, required_scope=self.required_scope)
        spec = self._by_target.get(offer.target)
        if spec is None or dict(offer.fields) != proposal.args:
            return self._refused()
        current = context.obs
        if urlsplit(current.url).path != spec.entry_route or current.page_route != spec.entry_route or current.render_state != "ready":
            return self._refused()
        entry = self.forms.plan_entry(spec.entry_reference, current, context=context.operation)
        opened = context.page.execute("click", {"ref": entry.control.ref})
        if opened.state != "completed" or opened.receipt is None:
            self.forms.discard_entry(entry, context=context.operation)
            return self._refused()
        reference = self.forms.accept_entry(entry, current, opened.receipt, context.obs, context=context.operation)
        form = self.forms.describe_form(context.obs, context=context.operation)
        if form is None or form.reference != reference or form.route != offer.target or form.submit_label != spec.submit_label:
            return self._refused()
        fields = {field.reference: field for field in form.fields}
        if not ({key for key, _ in offer.fields} <= fields.keys()):
            return self._refused()
        draft = self.forms.prepare_form(form, tuple(ApplicationFormValue(fields[key], value, "textbox") for key, value in offer.fields), context=context.operation)
        if draft.persistence != "transient_until_submit" or draft.route != offer.target or {item.field: item.value for item in draft.fields} != dict(offer.fields):
            return self._refused()
        before, expected = context.obs, []
        for item in draft.fields:
            matches = [
                e for e in context.obs.elements
                if e.role == item.control and e.attrs.get("form_scope") == draft.control_scope
                and (e.attrs.get("field") == item.field if item.binding == "field" else e.attrs.get("field_label") == item.label)
            ]
            if len(matches) != 1:
                return self._refused()
            control = matches[0]
            if context.page.execute("fill", {"ref": control.ref, "value": item.value, "commit": True}).state != "completed":
                return self._refused()
            expected.append((field_identity(control), item.value))
        return handoff_draft(context, before, expected, draft.submit_label)


class DeclarativeDraftSteps:
    """Declarative ``ApplicationDraftStepPort`` for explicit-save forms and local lines."""

    actions = frozenset({"click", "fill", "select"})

    def __init__(self, forms: tuple[FormSpec, ...], *, application_id: str, required_scope: str, actor_verifier: Callable[[object, Callable[[], None]], None]):
        self.application_id, self.required_scope, self._forms, self._actor_verifier = application_id, required_scope, tuple(forms), actor_verifier
        self._prepared: dict[str, tuple] = {}
        self._draft_routes = tuple(p for form in self._forms for p in form.draft_routes)
        self._scopes = frozenset(s for form in self._forms for s in (form.control_scope, form.edit_control_scope) if s)

    @classmethod
    def from_adapter(cls, adapter) -> "DeclarativeDraftSteps":
        return cls(
            tuple(adapter.spec.forms), application_id=adapter.spec.application_id, required_scope=adapter.read_scope,
            actor_verifier=lambda ctx, cp: (cp(), adapter.identity.verify_live_actor(ctx, required_scope=adapter.read_scope, mismatch_error_cls=PrincipalError, mismatch_message="action actor differs from delegated API actor")),
        )

    def _draft_route(self, route: str) -> bool:
        return any(_route_matches(route, pattern) for pattern in self._draft_routes)

    def _form_for_observation(self, observation) -> FormSpec | None:
        return next((f for f in self._forms if any(_route_matches(observation.page_route, p) for p in f.draft_routes)), self._forms[0] if len(self._forms) == 1 else None)

    def handles(self, intent) -> bool:
        return intent.action in self.actions and self._draft_route(intent.observation.page_route)

    def _controls(self, observation):
        return [e for e in observation.elements if e.attrs.get("form_scope") in self._scopes]

    def prepare_step(self, intent, *, context, checkpoint):
        self._actor_verifier(context, checkpoint)
        before = intent.observation
        element = next(e for e in before.elements if e.ref == intent.arguments["ref"])
        controls, form = self._controls(before), self._form_for_observation(before)
        field_labels = {f.name: f.label for f in form.fields} if form else {}
        label = element.attrs.get("field_label") or field_labels.get(element.attrs.get("field"), "")
        instance = element.attrs.get("form_instance", "")
        if element not in controls or not instance or element.attrs.get("disabled") == "true" or element.attrs.get("readonly") == "true":
            raise ActionAdmissionRefused("unreviewed")
        args, prefix = intent.arguments, form.local_line_prefix if form else ""
        is_line = bool(prefix and label.startswith(prefix) and label.removeprefix(prefix).isdigit())
        choice_fields = {f.label for f in form.fields if f.choices} if form else set()
        text_fields = {f.label for f in form.fields if not f.choices and f.updateable} if form else set()
        if intent.action in {"fill", "select"}:
            is_choice = intent.action == "select" and label in choice_fields and element.role == "combobox" and bool(element.attrs.get("aria-controls"))
            if not is_choice and (intent.action != "fill" or element.tag != "input" or (label not in text_fields and not is_line) or args.get("commit", True) is not True):
                raise ActionAdmissionRefused("unsafe_effect")
            step, values = "enter_value", ((label, args["value"]),)
        elif form and form.add_line_button and element.tag == "button" and element.name == form.add_line_button:
            step, values = "add_line", ()
        else:
            raise ActionAdmissionRefused("unsafe_effect")
        line_count = sum(bool(prefix) and e.attrs.get("field_label", "").startswith(prefix) for e in controls)
        expected = (step, label, args.get("value", ""), instance, line_count, element.attrs.get("form_lifetime", ""))
        self._prepared[intent.binding_digest] = expected
        digest = hashlib.sha256(json.dumps(expected).encode()).hexdigest()
        scope_key = form.control_scope.split(":", 1)[-1] if form and form.control_scope else "form"
        return DraftStepPermit(intent.binding_digest, f"{self.application_id}-explicit-save-drafts-v1", digest, scope_key, instance, step, values)

    def verify_step(self, permit, intent, receipt, *, observation, context, checkpoint):
        self._actor_verifier(context, checkpoint)
        expected = self._prepared.get(intent.binding_digest)
        if expected is None or not self._draft_route(observation.page_route):
            return False
        step, label, value, instance, count, lifetime = expected
        form = self._form_for_observation(observation)
        prefix = form.local_line_prefix if form else ""
        if step == "add_line":
            if not lifetime or not prefix:
                return False
            controls = [e for e in self._controls(observation) if e.attrs.get("form_lifetime") == lifetime]
            return sum(e.attrs.get("field_label", "").startswith(prefix) for e in controls) == count + 1
        controls = [e for e in self._controls(observation) if e.attrs.get("form_instance") == instance]
        field_name = {f.label: f.name for f in form.fields}.get(label, "") if form else ""
        matches = [e for e in controls if e.attrs.get("field_label") == label or (field_name and e.attrs.get("field") == field_name)]
        return len(matches) == 1 and matches[0].attrs.get("selected_option" if intent.action == "select" else "value", "") == value

    def verify_no_effect(self, permit, intent, receipt, *, observation, context, checkpoint):
        self._actor_verifier(context, checkpoint)
        if intent.action != "select" or not receipt.no_effect or observation.url != intent.observation.url or not self._draft_route(observation.page_route):
            return False
        state = lambda obs: sorted((field_identity(e), field_value(e), e.attrs.get("aria-controls", "")) for e in draft_fields(obs))
        before, after = state(intent.observation), state(observation)
        return bool(before) and before == after

    def may_continue_unverified(self, permit, intent, receipt, *, observation, context, checkpoint):
        self._actor_verifier(context, checkpoint)
        return intent.binding_digest in self._prepared and observation.url == intent.observation.url and self._draft_route(observation.page_route)


class DeclarativeReadActions:
    """Declarative ``ActionAdmission`` policy for reviewed navigation, tabs, and query forms."""

    actions = frozenset({"goto", "click", "fill"})

    def __init__(
        self, *, application_id: str, origin: str, required_scope: str,
        actor_verifier: Callable[[object, Callable[[], None]], None], pages: tuple[PageSpec, ...] = (),
        allowed_routes: frozenset[str] = frozenset(), filters: bool = False,
    ):
        self.application_id, self.origin, self.required_scope, self._actor_verifier = application_id, origin.rstrip("/"), required_scope, actor_verifier
        self._pages, self._filters = {page.route: page for page in pages}, bool(filters)
        self._query_forms = {page.query_form.route: page.query_form for page in pages if page.query_form is not None}
        self._allowed_destinations = frozenset(set(allowed_routes) | {p.route for p in pages if filters or p.query_form is None})
        self._queries: dict[str, tuple] = {}
        self.review = f"{application_id}-navigation-v1"

    @classmethod
    def from_adapter(cls, adapter, *, allowed_routes: frozenset[str] = frozenset(), filters: bool = False) -> "DeclarativeReadActions":
        return cls(
            application_id=adapter.spec.application_id, origin=adapter.descriptor.origin, required_scope=adapter.read_scope,
            actor_verifier=lambda ctx, cp: (cp(), adapter.identity.verify_live_actor(ctx, required_scope=adapter.read_scope, mismatch_error_cls=PrincipalError, mismatch_message="action actor differs from delegated API actor")),
            pages=tuple(adapter.spec.pages), allowed_routes=allowed_routes, filters=filters,
        )

    def _path(self, url: str) -> str:
        parsed = urlsplit(url)
        if f"{parsed.scheme}://{parsed.netloc}" != self.origin or parsed.query or parsed.fragment:
            raise ActionAdmissionRefused("invalid_target")
        return parsed.path

    def admit(self, intent, *, context, checkpoint):
        self._actor_verifier(context, checkpoint)
        before = intent.observation
        if before.page_route != self._path(before.url):
            raise ActionAdmissionRefused("invalid_target")
        if self._filters and before.page_route in self._query_forms and intent.action != "goto":
            return self._admit_query(intent, self._query_forms[before.page_route])
        pspec = self._pages.get(before.page_route)
        if intent.action == "click" and pspec is not None and pspec.section_tab_ids:
            target = next(e for e in before.elements if e.ref == intent.arguments["ref"])
            if target.role == "tab" and target.tag == "button" and target.attrs.get("id") in pspec.section_tab_ids:
                return ActionPermit(intent.binding_digest, ActionEffect.READ_ONLY, f"{self.application_id}-tabs-v1", target.attrs["id"])
            raise ActionAdmissionRefused("unreviewed")
        if intent.action == "goto":
            destination = self._path(urljoin(before.url, intent.arguments["url"]))
        else:
            element = next(e for e in before.elements if e.ref == intent.arguments["ref"])
            if element.role != "link" or element.tag != "a" or element.attrs.get("form_scope") or element.attrs.get("target", "") not in {"", "_self"} or not element.attrs.get("href"):
                raise ActionAdmissionRefused("unreviewed")
            destination = self._path(urljoin(before.url, element.attrs["href"]))
        if destination not in self._allowed_destinations:
            raise ActionAdmissionRefused("unreviewed")
        return ActionPermit(intent.binding_digest, ActionEffect.NAVIGATION, self.review, destination)

    def verify(self, permit, intent, receipt, *, observation, context, checkpoint):
        self._actor_verifier(context, checkpoint)
        after = observation
        if permit.review == f"{self.application_id}-tabs-v1":
            target = next(e for e in intent.observation.elements if e.ref == intent.arguments["ref"])
            selected = [e for e in after.elements if e.role == "tab" and e.name == target.name and e.attrs.get("id") == permit.postcondition]
            return after.url == intent.observation.url and after.page_route == intent.observation.page_route and len(selected) == 1 and selected[0].attrs.get("aria-selected") == "true"
        if intent.binding_digest in self._queries:
            qform, value, instance, revision = self._queries.pop(intent.binding_digest)
            fields = [e for e in after.elements if e.attrs.get(qform.field_attr) == qform.field_name and e.attrs.get("form_scope") == qform.form_scope and e.attrs.get("form_instance") == instance]
            if after.url != intent.observation.url or after.page_route != qform.route or len(fields) != 1 or fields[0].attrs.get("value", "") != value:
                return False
            if intent.action == "fill":
                return after.page_read == intent.observation.page_read
            read = after.page_read
            if read is None or read["revision"] <= revision:
                self._queries[intent.binding_digest] = (qform, value, instance, revision)
                raise ActionResultPending()
            digest = hashlib.sha256(json.dumps([value], separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
            return read["path"] == qform.api_path and read["query_fields"] == {qform.field_name: digest} and read["page"] == 1
        return self._path(after.url) == permit.postcondition and after.page_route == permit.postcondition

    def _admit_query(self, intent, qform: QueryFormSpec):
        before, args = intent.observation, intent.arguments
        target = next(e for e in before.elements if e.ref == args["ref"])
        fields = [e for e in before.elements if e.attrs.get(qform.field_attr) == qform.field_name and e.attrs.get("form_scope") == qform.form_scope]
        if len(fields) != 1 or before.page_read is None:
            raise ActionAdmissionRefused("unreviewed")
        field = fields[0]
        if target.attrs.get("form_scope") != qform.form_scope or not field.attrs.get("form_instance") or target.attrs.get("form_instance") != field.attrs["form_instance"]:
            raise ActionAdmissionRefused("stale_binding")
        if intent.action == "fill" and target == field and args.get("commit", True) is True:
            value = args["value"]
        elif intent.action == "click" and target.tag == "button" and target.name == qform.submit_label:
            value = field.attrs.get("value", "")
        else:
            raise ActionAdmissionRefused("unreviewed")
        self._queries[intent.binding_digest] = (qform, value, field.attrs["form_instance"], before.page_read["revision"])
        return ActionPermit(intent.binding_digest, ActionEffect.READ_ONLY, f"{self.application_id}-query-v1", intent.binding_digest)


__all__ = ["DeclarativeDraftSteps", "DeclarativeFormPort", "DeclarativeMutationPrefill", "DeclarativeReadActions"]
