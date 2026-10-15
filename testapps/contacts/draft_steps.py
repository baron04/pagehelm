"""Reviewed plain-HTML local drafts; the fixture persists only on explicit Save."""
import hashlib
import json

from web_operation_agent.business.draft_steps import DraftStepPermit
from web_operation_agent.execution.action_admission import ActionAdmissionRefused
from .adapter import APPLICATION_ID
from .read_actions import ContactsReadActions


class ContactsDraftSteps:
    application_id = APPLICATION_ID
    required_scope = "contacts:read"
    actions = frozenset({"click", "fill", "select"})

    def __init__(self, adapter):
        self.actor = ContactsReadActions(adapter)
        self._prepared = {}

    def handles(self, intent):
        return intent.action in self.actions and intent.observation.page_route == "/contacts/new"

    @staticmethod
    def _controls(observation):
        return [e for e in observation.elements if e.attrs.get("form_scope") == "form:contact-create"]

    def prepare_step(self, intent, *, context, checkpoint):
        self.actor._actor(context, checkpoint)
        before = intent.observation
        element = next(e for e in before.elements if e.ref == intent.arguments["ref"])
        controls = self._controls(before)
        label = element.attrs.get("field_label", "")
        instance = element.attrs.get("form_instance", "")
        if (element not in controls or not instance or element.attrs.get("disabled") == "true"
                or element.attrs.get("readonly") == "true"):
            raise ActionAdmissionRefused("unreviewed")
        args = intent.arguments
        line = label.startswith("Local line ") and label.removeprefix("Local line ").isdigit()
        if intent.action in {"fill", "select"}:
            choice = (intent.action == "select" and label == "Priority"
                      and element.role == "combobox" and element.attrs.get("aria-controls") == "contact-priorities")
            if (not choice and (intent.action != "fill" or element.tag != "input"
                    or label not in {"Period start", "Period end"} and not line
                    or args.get("commit", True) is not True)):
                raise ActionAdmissionRefused("unsafe_effect")
            step, values = "enter_value", ((label, args["value"]),)
        elif element.tag == "button" and element.name == "Add local line":
            step, values = "add_line", ()
        else:
            raise ActionAdmissionRefused("unsafe_effect")
        count = sum(e.attrs.get("field_label", "").startswith("Local line ") for e in controls)
        expected = (step, label, args.get("value", ""), instance, count, element.attrs.get("form_lifetime", ""))
        self._prepared[intent.binding_digest] = expected
        digest = hashlib.sha256(json.dumps(expected).encode()).hexdigest()
        return DraftStepPermit(intent.binding_digest, "contacts-explicit-save-drafts-v1", digest,
                               "contact-create", instance, step, values)

    def verify_step(self, permit, intent, receipt, *, observation, context, checkpoint):
        self.actor._actor(context, checkpoint)
        expected = self._prepared.get(intent.binding_digest)
        if expected is None or observation.page_route != "/contacts/new":
            return False
        step, label, value, instance, count, lifetime = expected
        controls = [e for e in self._controls(observation) if e.attrs.get("form_instance") == instance]
        if step == "add_line":
            if not lifetime: return False
            controls = [e for e in self._controls(observation) if e.attrs.get("form_lifetime") == lifetime]
            return sum(e.attrs.get("field_label", "").startswith("Local line ") for e in controls) == count + 1
        matches = [e for e in controls if e.attrs.get("field_label") == label]
        return len(matches) == 1 and matches[0].attrs.get(
            "selected_option" if intent.action == "select" else "value", "") == value

    def verify_no_effect(self, permit, intent, receipt, *, observation, context, checkpoint):
        self.actor._actor(context, checkpoint)
        from web_operation_agent.execution.draft_handoff import draft_fields, field_identity, field_value
        if (intent.action != "select" or not receipt.no_effect or observation.url != intent.observation.url
                or observation.page_route != "/contacts/new"):
            return False
        def state(obs):
            return sorted((field_identity(e), field_value(e), e.attrs.get("aria-controls", ""))
                          for e in draft_fields(obs))
        before, after = state(intent.observation), state(observation)
        return bool(before) and before == after
