"""Reviewed customer editor admission, issued by the server after an exact click receipt.

Deployment-owned policies admit customer updates and defined creation entries. File hashes
bind an entry to the reviewed source bytes; live APIs attest the actor's authority. DOM tokens attest
only lifetime. None of those facts alone authorizes filling or submission.
"""
from dataclasses import dataclass
import hashlib
import re
from urllib.parse import urlsplit

from web_operation_agent.business.errors import (BusinessToolRequestError,
    BusinessToolPermissionError, BusinessToolIdentityError, BusinessToolUnavailableError)
from web_operation_agent.business.forms import ApplicationFormEntry
from web_operation_agent.business.form_entries import FormEntryCandidate
from web_operation_agent.goal.data import DataRefused
from web_operation_agent.planning.sourcetree import AccessDenied
from web_operation_agent.business.tools.contracts import form_control_unavailable, matches_form_control
from .forms import NativeFormState, YudaoFormPort, source_field_controls
from .source_forms import SourceFormReader
from web_operation_agent.schemas.browser import FORM_INPUT_CONTROLS

REVIEWED_FILES = (
    "ui/src/views/crm/customer/detail/index.vue",
    "ui/src/views/crm/customer/CustomerForm.vue",
    "ui/src/components/Dialog/src/Dialog.vue",
    "ui/src/views/crm/permission/components/PermissionList.vue",
)
_TARGET = re.compile(r"customer-update:([1-9][0-9]{0,19})\Z")
_INSTANCE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")


def _location(url):
    parts = urlsplit(url)
    return parts.path + ("?" + parts.query if parts.query else "")


def _creation_location(review, url):
    parts = urlsplit(url)
    # The reviewed PMS opener belongs to these source-declared collection tabs.
    # Extra parameters and duplicate keys are not an entry identity.
    return (parts.path == review.entry[1] and not parts.fragment
            and (not parts.query or review.reference == "project-create"
                 and parts.query in {"tabs=participate", "tabs=owner", "tabs=all"}))


@dataclass(frozen=True)
class NativeFormReview:
    release: str
    hashes: tuple[tuple[str, str], ...]

    @classmethod
    def load(cls, payload, tree):
        if (not isinstance(payload, dict) or set(payload) != {"schema_version", "release", "hashes"}
                or type(payload["schema_version"]) is not int or payload["schema_version"] != 1
                or payload["release"] != tree.release
                or not isinstance(payload["hashes"], dict)
                or set(payload["hashes"]) != set(REVIEWED_FILES)
                or any(not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v)
                       for v in payload["hashes"].values())):
            raise ValueError("native form review must pin every reviewed file of this release")
        review = cls(payload["release"], tuple(sorted(payload["hashes"].items())))
        if not review.matches(tree):
            raise ValueError("native form review does not match current source bytes")
        return review

    def matches(self, tree):
        try:
            return (self.release == tree.release and all(
                hashlib.sha256(tree.read_text(path).encode()).hexdigest() == sha
                for path, sha in self.hashes))
        except (AccessDenied, OSError, UnicodeError):
            return False


class NativeCustomerEntryVerifier:
    def __init__(self, tree, review, client):
        if type(self) is NativeCustomerEntryVerifier and (
                type(review) is not NativeFormReview or {p for p, _ in review.hashes} != set(REVIEWED_FILES)):
            raise TypeError("customer updates require their own complete source review")
        self.tree, self.review, self.client = tree, review, client
        self.source = SourceFormReader(tree)
        self._planned = None
        self._current = None

    def _authorize(self, record, context):
        context.require_live(application_id="yudao", required_scope="yudao.read")
        if not self.review.matches(self.tree):
            raise BusinessToolRequestError("the reviewed native source changed")
        try:
            identity = self.client.get("system/auth/get-permission-info")
            if not isinstance(identity, dict):
                raise BusinessToolIdentityError("the editor identity is unavailable")
            user = identity.get("user") or {}
            if (not isinstance(user, dict) or str(user.get("id")) != context.principal.actor_id
                    or str(self.client.tenant) != context.principal.tenant_id):
                raise BusinessToolIdentityError("the editor actor changed")
            permissions = identity.get("permissions") or []
            team = self.client.get("crm/permission/list", bizType=2, bizId=record)
            customer = self.client.get("crm/customer/get", id=record)
        except DataRefused:
            raise BusinessToolPermissionError("the application refused the editor read") from None
        except (OSError, TimeoutError):
            raise BusinessToolUnavailableError("the editor authority could not be verified") from None
        if (not isinstance(customer, dict) or str(customer.get("id")) != record
                or not isinstance(team, list) or not isinstance(permissions, list)
                or any(not isinstance(permission, str) for permission in permissions)
                or not {"crm:customer:update", "*:*:*"}.intersection(permissions)
                or not any(isinstance(row, dict) and str(row.get("userId")) == context.principal.actor_id
                           and type(row.get("bizType")) is int and row["bizType"] == 2
                           and str(row.get("bizId")) == record
                           and type(row.get("level")) is int and row["level"] in (1, 3) for row in team)):
            raise BusinessToolPermissionError("this actor cannot edit this customer")

    def plan_entry(self, reference, observation, *, context):
        self._planned = self._current = None
        match = _TARGET.fullmatch(reference)
        if not match:
            raise BusinessToolRequestError("this reviewed editor target is unavailable")
        record = match[1]
        route = "/crm/customer/detail/" + record
        if (observation.render_state != "ready" or observation.record_route != route
                or urlsplit(observation.url).path != route or urlsplit(observation.url).query
                or any(str(e.attrs.get("form_scope", "")).startswith("dialog") for e in observation.elements)):
            raise BusinessToolRequestError("the exact customer page must be ready with no open editor")
        self._authorize(record, context)
        controls = [e for e in observation.elements if e.role == "button" and e.name == "编辑"
                    and not e.attrs.get("form_scope") and not form_control_unavailable(e.attrs)]
        if len(controls) != 1:
            raise BusinessToolRequestError("the reviewed editor opener is not unique and enabled")
        entry = ApplicationFormEntry(reference, controls[0])
        self._planned = entry, context.principal.binding_digest, context.session_id, context.turn_id, route, route
        return entry

    @staticmethod
    def _scope(observation):
        # Calendar/list popups may also use role=dialog. Their navigation buttons do
        # not constitute another editor: bind the actual field controls and the
        # reviewed submit control. A second labelled editor or submit stays ambiguous.
        scopes = {(e.attrs.get("form_scope"), e.attrs.get("form_instance"))
                  for e in observation.elements
                  if (e.role in FORM_INPUT_CONTROLS and
                      (e.attrs.get("field") or e.attrs.get("field_label")))
                  or (e.role == "button" and e.name == "确 定")
                  if isinstance(e.attrs.get("form_scope"), str)
                  and e.attrs["form_scope"].startswith("dialog")}
        if len(scopes) != 1:
            raise BusinessToolRequestError("the current editor scope is ambiguous")
        scope, instance = next(iter(scopes))
        if not isinstance(instance, str) or not _INSTANCE.fullmatch(instance):
            raise BusinessToolRequestError("the current editor has no live instance")
        return scope, instance

    def _checked_entry(self, planned, entry, before, receipt, observation, context):
        if (planned is None or planned[:4] != (entry, context.principal.binding_digest,
                context.session_id, context.turn_id) or entry.control not in before.elements
                or not receipt.ok or receipt.action != "click" or receipt.navigated
                or not receipt.write_guard_held
                or receipt.args_echo != {"ref": entry.control.ref, "epoch": before.epoch}
                or observation.epoch <= before.epoch or observation.render_state != "ready"
                or observation.record_route != planned[5]
                or _location(observation.url) != planned[4]):
            raise BusinessToolRequestError("the editor receipt did not bind this entry")
        return planned, *self._scope(observation)

    def _entry_state(self, planned, entry, before, receipt, observation, context):
        planned, scope, instance = self._checked_entry(
            planned, entry, before, receipt, observation, context)
        record = _TARGET.fullmatch(entry.reference)[1]
        self._authorize(record, context)
        source = self.source.read(REVIEWED_FILES[1])
        if source is None:
            raise BusinessToolRequestError("the reviewed editor source is unavailable")
        return NativeFormState(context.principal.actor_authority_digest,
            context.session_id, context.turn_id, instance, "update", record, planned[4], scope,
            REVIEWED_FILES[1], source.binding_digest, "客户编辑", "确 定")

    def verify_entry(self, entry, before, receipt, observation, *, context):
        """Check the guarded opener without consuming FormPort's pending binding."""
        self._entry_state(self._planned, entry, before, receipt, observation, context)
        return True

    def accept_entry(self, entry, before, receipt, observation, *, context):
        planned, self._planned = self._planned, None
        self._current = self._entry_state(planned, entry, before, receipt, observation, context)
        return "native-" + self._current.digest

    def discard_entry(self, entry, *, context):
        if self._planned is not None and self._planned[0] == entry:
            self._planned = None

    def current(self, observation, *, context):
        state = self._current
        if state is None:
            return None
        try:
            scope, instance = self._scope(observation)
            if (observation.render_state != "ready" or observation.record_route != (state.route if state.mode == "update" else "")
                    or _location(observation.url) != state.route
                    or (scope, instance) != (state.control_scope, state.entry_id)):
                self._current = None
                return None
            if (state.actor_digest != context.principal.actor_authority_digest
                    or state.session_id != context.session_id or state.turn_id != context.turn_id):
                self._current = None
                return None
        except (BusinessToolRequestError, BusinessToolPermissionError, BusinessToolIdentityError):
            self._current = None
            raise
        return state

    def validate(self, state, *, context):
        if (state != self._current or state.actor_digest != context.principal.actor_authority_digest
                or state.session_id != context.session_id or state.turn_id != context.turn_id):
            return False
        try:
            self._authorize(state.record_id, context)
        except (BusinessToolRequestError, BusinessToolPermissionError,
                BusinessToolIdentityError, BusinessToolUnavailableError):
            self._current = None
            raise
        return True


# Explicit application distributions qualify each opener and persistence contract.
# These declarations stay on the server; no app-specific schema enters the Agent.
CREATE_ENTRIES = {
    "contact-create": ("crm/contact", "/crm/contact", "创建联系人", "crm:contact:create",
        "ui/src/views/crm/contact/components/ContactList.vue",
        "ui/src/views/crm/contact/ContactForm.vue"),
    "customer-create": ("crm/customer", "/crm/customer", "新增", "crm:customer:create",
        "ui/src/views/crm/customer/index.vue", REVIEWED_FILES[1]),
    "project-create": ("pms/pm/project", "/pms/pm/project/list", "新建项目", "pms:pm:project:create",
        "ui/src/views/pms/pm/project/list/index.vue",
        "ui/src/views/pms/pm/project/components/ProjectForm.vue"),
}


@dataclass(frozen=True)
class NativeCreateReview(NativeFormReview):
    reference: str
    definition: tuple[str, ...] = ()

    @property
    def entry(self):
        if self.definition:
            return self.definition
        if self.reference not in CREATE_ENTRIES:
            raise ValueError("creation requires a reviewed entry definition")
        return CREATE_ENTRIES[self.reference]

    @classmethod
    def load(cls, payload, tree):
        required_keys = {"schema_version", "release", "hashes", "reference"}
        if (not isinstance(payload, dict) or set(payload) not in (required_keys, required_keys | {"entry"})
                or type(payload["schema_version"]) is not int or payload["schema_version"] != 1
                or payload["release"] != tree.release or not isinstance(payload["reference"], str)
                or not re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", payload["reference"])
                or not isinstance(payload["hashes"], dict)):
            raise ValueError("native creation needs a named reviewed entry")
        definition = ()
        if "entry" in payload:
            raw = payload["entry"]
            keys = ("entity", "route", "label", "permission", "parent_component", "form_component")
            if (not isinstance(raw, dict) or set(raw) != set(keys)
                    or any(not isinstance(raw[k], str) for k in keys)
                    or not re.fullmatch(r"[a-z][a-z0-9_]*(?:/[a-z][a-z0-9_]*){1,5}", raw["entity"])
                    or not re.fullmatch(r"/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+){0,10}", raw["route"])
                    or not raw["label"].strip() or len(raw["label"]) > 240
                    or re.search(r"[\x00-\x1f\x7f]", raw["label"])
                    or not re.fullmatch(r"[a-z][a-z0-9_-]*(?::[a-z][a-z0-9_-]*){1,5}", raw["permission"])
                    or any(not re.fullmatch(r"ui/src/views/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.vue", raw[k])
                           for k in ("parent_component", "form_component"))):
                raise ValueError("creation entry definition is malformed")
            definition = tuple(raw[k] for k in keys)
            if payload["reference"] in CREATE_ENTRIES and definition != CREATE_ENTRIES[payload["reference"]]:
                raise ValueError("a built-in entry cannot borrow another definition")
        elif payload["reference"] not in CREATE_ENTRIES:
            raise ValueError("creation requires a reviewed entry definition")
        entry = definition or CREATE_ENTRIES[payload["reference"]]
        required = {entry[4], entry[5], REVIEWED_FILES[2]}
        if payload["reference"] == "contact-create":
            required.add(REVIEWED_FILES[0])
        if (set(payload["hashes"]) != required or any(not isinstance(v,str)
                or not re.fullmatch(r"[0-9a-f]{64}",v) for v in payload["hashes"].values())):
            raise ValueError("native creation must pin its own opener, form and dialog")
        review = cls(payload["release"], tuple(sorted(payload["hashes"].items())), payload["reference"], definition)
        if not review.matches(tree):
            raise ValueError("native creation review does not match current source")
        return review


class NativeCreateEntryVerifier(NativeCustomerEntryVerifier):
    def __init__(self, tree, review, client):
        if not isinstance(review, NativeCreateReview):
            raise TypeError("creation cannot use a customer update review")
        super().__init__(tree, review, client)
        self.goal_entries = ((review.reference, *review.entry),)
        self._adoption_turn = None
        self._adopted = None

    def adopt_current(self, observation, *, context):
        """Bind one user-opened creation to reviewed mode evidence and live identity.

        A route or source hash alone proves no mode. A source-declared create-only
        field must be uniquely writable in this editor; the contact parent path has
        its separately reviewed owner discriminator. No opener receipt is fabricated.
        Once adopted, a changed or lost binding cannot be readopted in the same turn.
        """
        turn = context.session_id, context.turn_id
        if self._adoption_turn == turn and self._adopted is None:
            return None
        state = self._observed_creation(observation, context=context)
        if self._adoption_turn == turn and state != self._adopted:
            self._current = self._adopted = None
            return None
        if state is not None:
            self._adoption_turn, self._adopted = turn, state
        self._current = state
        return state

    def _observed_creation(self, observation, *, context):
        path = urlsplit(observation.url).path
        if self.review.reference == "contact-create" and path.startswith("/crm/customer/detail/"):
            return self._observed_parent_contact(observation, context=context)
        if (observation.render_state != "ready" or observation.record_route
                or observation.page_route != path or not _creation_location(self.review, observation.url)):
            return None
        component = self.review.entry[5]
        source = self.source.read(component)
        if source is None:
            return None
        markers = [f for f in source.fields if f.conditional and f.modes == ("create",) and not f.handlers]
        if not markers:
            return None
        # No editor is a normal unsupported observation, not an ambiguous editor.
        if not any(e.role in FORM_INPUT_CONTROLS and str(e.attrs.get("form_scope", "")).startswith("dialog")
                   for e in observation.elements):
            return None
        scope, instance = self._scope(observation)
        for field in markers:
            controls = [e for e in observation.elements
                        if any(matches_form_control(e, field=field.reference, label=field.label,
                            control=role, binding="label", scope=scope)
                            for role in source_field_controls(field.input_type))]
            if len(controls) != 1 or form_control_unavailable(controls[0].attrs):
                return None
        self._authorize("", context)
        return NativeFormState(context.principal.actor_authority_digest,
            context.session_id, context.turn_id, instance, "create", "", _location(observation.url), scope,
            component, source.binding_digest, "新建草稿", "确 定")

    def _observed_parent_contact(self, observation, *, context):
        # Its enabled owner distinguishes creation from the update editor. Reviewed
        # parent/opener bytes bind the unobserved disabled customer to this record.
        path = urlsplit(observation.url).path
        match = re.fullmatch(r"/crm/customer/detail/([1-9][0-9]{0,19})", path)
        if (not match or urlsplit(observation.url).query or observation.render_state != "ready"
                or observation.record_route != path):
            return None
        scope, instance = self._scope(observation)
        def fields(label):
            return [e for e in observation.elements if e.role in FORM_INPUT_CONTROLS
                    and e.attrs.get("form_scope") == scope
                    and e.attrs.get("field_label") == label]
        name, mobile, owner = (fields(label) for label in ("联系人姓名", "手机", "负责人"))
        if (any(len(items) != 1 for items in (name, mobile, owner))
                or any(form_control_unavailable(e.attrs) for e in (*name, *mobile, *owner))):
            return None
        self._authorize("", context)
        # Read the parent under this actor, never derive its authority from a URL.
        parent = self.client.get("crm/customer/get", id=match[1])
        if not isinstance(parent, dict) or str(parent.get("id")) != match[1]:
            raise BusinessToolPermissionError("the contact parent is not readable")
        component = self.review.entry[5]
        source = self.source.read(component)
        if source is None:
            return None
        return NativeFormState(context.principal.actor_authority_digest,
            context.session_id, context.turn_id, instance, "create", "", path, scope,
            component, source.binding_digest, "新建联系人草稿", "确 定")

    def current(self, observation, *, context):
        if self._adoption_turn == (context.session_id, context.turn_id):
            return self.adopt_current(observation, context=context)
        state = super().current(observation, context=context)
        return state if state is not None else self.adopt_current(observation, context=context)

    def _authorize(self, record, context):
        context.require_live(application_id="yudao", required_scope="yudao.read")
        if record or not self.review.matches(self.tree):
            raise BusinessToolRequestError("the reviewed creation target changed")
        try:
            identity = self.client.get("system/auth/get-permission-info")
        except DataRefused:
            raise BusinessToolPermissionError("the application refused creation authority") from None
        except (OSError, TimeoutError):
            raise BusinessToolUnavailableError("creation authority is unavailable") from None
        user = identity.get("user") if isinstance(identity, dict) else None
        permissions = identity.get("permissions") if isinstance(identity, dict) else None
        if (not isinstance(user, dict) or str(user.get("id")) != context.principal.actor_id
                or str(self.client.tenant) != context.principal.tenant_id):
            raise BusinessToolIdentityError("the creation actor changed")
        required = self.review.entry[3]
        if (not isinstance(permissions,list) or any(not isinstance(p,str) for p in permissions)
                or not {required,"*:*:*"}.intersection(permissions)):
            raise BusinessToolPermissionError("this actor cannot create this entity")

    def plan_entry(self, reference, observation, *, context):
        self._planned = self._current = None
        if reference != self.review.reference:
            raise BusinessToolRequestError("this creation entry is not reviewed")
        _, route, label, *_ = self.review.entry
        if (observation.render_state != "ready" or observation.record_route
                or not _creation_location(self.review, observation.url)
                or any(str(e.attrs.get("form_scope", "")).startswith("dialog") for e in observation.elements)):
            raise BusinessToolRequestError("the exact collection must be ready without an editor")
        self._authorize("",context)
        controls = [e for e in observation.elements if e.role == "button" and e.name == label
                    and not str(e.attrs.get("form_scope", "")).startswith("dialog")
                    and not form_control_unavailable(e.attrs)]
        if len(controls) != 1:
            raise BusinessToolRequestError("the reviewed creation opener is not unique and enabled")
        entry = ApplicationFormEntry(reference,controls[0])
        self._planned = entry,context.principal.binding_digest,context.session_id,context.turn_id,_location(observation.url),""
        return entry

    def _entry_state(self, planned, entry, before, receipt, observation, context):
        planned, scope, instance = self._checked_entry(
            planned, entry, before, receipt, observation, context)
        self._authorize("",context)
        component = self.review.entry[5]
        source = self.source.read(component)
        if source is None:
            raise BusinessToolRequestError("the reviewed creation source is unavailable")
        return NativeFormState(context.principal.actor_authority_digest,context.session_id,
            context.turn_id,instance,"create","",planned[4],scope,component,source.binding_digest,"新建草稿","确 定")


class NativeEntryVerifiers:
    """One current entry among explicitly installed application-owned policies."""
    def __init__(self, *verifiers):
        if not verifiers or any(not isinstance(v,NativeCustomerEntryVerifier) for v in verifiers):
            raise TypeError("native entries require reviewed application verifiers")
        bindings = {(v.tree.root, v.tree.release, v.tree.repository_id) for v in verifiers}
        references = [v.review.reference if isinstance(v, NativeCreateEntryVerifier)
                      else "customer-update" for v in verifiers]
        if len(bindings) != 1 or len(set(references)) != len(references):
            raise ValueError("native policies must share one source binding and unique targets")
        self.goal_updates = "customer-update" in references
        self.verifiers, self.active = tuple(verifiers), None
        self._active_turn = None
        self.source = verifiers[0].source
        self.goal_entries = tuple(e for v in verifiers for e in getattr(v,"goal_entries",()))

    @property
    def _planned(self):
        return self.active._planned if self.active else None

    def plan_entry(self, reference, observation, *, context):
        self.active = None
        self._active_turn = None
        for verifier in self.verifiers:
            verifier._planned = verifier._current = None
        candidates = [v for v in self.verifiers if (isinstance(v,NativeCreateEntryVerifier)
            and reference == v.review.reference) or (not isinstance(v,NativeCreateEntryVerifier) and _TARGET.fullmatch(reference))]
        if len(candidates) != 1:
            raise BusinessToolRequestError("the requested native entry is unavailable or ambiguous")
        self.active = candidates[0]
        self._active_turn = context.session_id, context.turn_id
        return self.active.plan_entry(reference,observation,context=context)

    def accept_entry(self, *args, **kwargs):
        if self.active is None: raise BusinessToolRequestError("no native entry was planned")
        return self.active.accept_entry(*args,**kwargs)

    def verify_entry(self, *args, **kwargs):
        if self.active is None: raise BusinessToolRequestError("no native entry was planned")
        return self.active.verify_entry(*args, **kwargs)

    def discard_entry(self, *args, **kwargs):
        if self.active: self.active.discard_entry(*args,**kwargs)

    def current(self, observation, *, context):
        turn = context.session_id, context.turn_id
        if self.active and self._active_turn == turn:
            return self.active.current(observation, context=context)
        # A new user turn may observe a different reviewed creation editor. Within
        # the bound turn, never fall back to another policy after losing its target.
        self.active = None
        matches = [(v, v.adopt_current(observation, context=context)) for v in self.verifiers
                   if isinstance(v, NativeCreateEntryVerifier)]
        matches = [(v, state) for v, state in matches if state is not None]
        if len(matches) != 1:
            return None
        self.active, state = matches[0]
        self._active_turn = turn
        return state

    def validate(self, *args, **kwargs):
        return self.active.validate(*args,**kwargs) if self.active else False


class NativeEntryFormPort(YudaoFormPort):
    def __init__(self, verifier):
        from .form_choices import NativeFormChoices
        super().__init__(verifier.source, verifier, choices=NativeFormChoices(verifier))
        self.goal_source = verifier.source
        self.goal_entries = getattr(verifier,"goal_entries",())
        self.goal_updates = getattr(verifier, "goal_updates",
            not isinstance(verifier, NativeCreateEntryVerifier))

    def current_goal_form(self, entity, observation, *, context):
        form = self.describe_form(observation, context=context)
        active = getattr(self._verifier, "active", self._verifier)
        if (form is not None and isinstance(active, NativeCreateEntryVerifier)
                and active.review.entry[0] == entity):
            return form
        return None

    def click_policy(self, tabs):
        return NativeEntryClickPolicy(self._verifier, tabs)

    def form_entries(self, observation, *, context):
        if (observation.render_state != "ready"
                or any(str(e.attrs.get("form_scope", "")).startswith("dialog")
                       for e in observation.elements)):
            return ()
        path = urlsplit(observation.url).path
        if observation.page_route != path:
            return ()
        candidates = []
        verifiers = (self._verifier.verifiers if isinstance(self._verifier, NativeEntryVerifiers)
                     else (self._verifier,))
        for verifier in verifiers:
            if isinstance(verifier, NativeCreateEntryVerifier):
                reference = verifier.review.reference
                _, route, label, *_ = verifier.review.entry
                if not _creation_location(verifier.review, observation.url) or observation.record_route:
                    continue
                record, kind = "", "create"
            else:
                match = re.fullmatch(r"/crm/customer/detail/([1-9][0-9]{0,19})", path)
                if not match or observation.record_route != path or urlsplit(observation.url).query:
                    continue
                record, kind = match[1], "update"
                reference, label = "customer-update:" + record, "编辑"
            controls = [e for e in observation.elements if e.role == "button" and e.name == label
                        and not str(e.attrs.get("form_scope", "")).startswith("dialog")
                        and not form_control_unavailable(e.attrs)]
            if len(controls) != 1:
                continue
            verifier._authorize(record, context)
            candidates.append(FormEntryCandidate(reference, label, kind))
        return tuple(candidates)

    def plan_entry(self, reference, observation, *, context):
        return self._verifier.plan_entry(reference, observation, context=context)

    def accept_entry(self, entry, before, receipt, observation, *, context):
        return self._verifier.accept_entry(entry, before, receipt, observation, context=context)

    def discard_entry(self, entry, *, context):
        self._verifier.discard_entry(entry, context=context)


class NativeEntryClickPolicy:
    """Only the currently issued exact opener, plus already qualified native tabs."""
    def __init__(self, verifier, tabs):
        self.verifier, self.tabs = verifier, tabs

    def decide(self, observation, element):
        planned = self.verifier._planned
        if (planned is not None and element == planned[0].control
                and element.epoch == observation.epoch and observation.render_state == "ready"
                and observation.record_route == planned[5]
                and _location(observation.url) == planned[4]):
            return ""
        return self.tabs.decide(observation, element) if self.tabs else "only a reviewed current form entry is allowed"
