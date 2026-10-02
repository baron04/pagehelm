"""Reviewed customer editor admission, issued by the server after an exact click receipt.

This distribution slice supports customer update only. File hashes attest reviewed
entry/persistence semantics; live APIs attest the actor's authority. DOM tokens attest
only lifetime. None of those facts alone authorizes filling or submission.
"""
from dataclasses import dataclass
import hashlib
import re
from urllib.parse import urlsplit

from web_operation_agent.business.errors import (BusinessToolRequestError,
    BusinessToolPermissionError, BusinessToolIdentityError, BusinessToolUnavailableError)
from web_operation_agent.business.forms import ApplicationFormEntry
from web_operation_agent.goal.data import DataRefused
from web_operation_agent.planning.sourcetree import AccessDenied
from web_operation_agent.business.tools.contracts import form_control_unavailable
from .forms import NativeFormState, YudaoFormPort
from .source_forms import SourceFormReader

REVIEWED_FILES = (
    "ui/src/views/crm/customer/detail/index.vue",
    "ui/src/views/crm/customer/CustomerForm.vue",
    "ui/src/components/Dialog/src/Dialog.vue",
    "ui/src/views/crm/permission/components/PermissionList.vue",
)
_TARGET = re.compile(r"customer-update:([1-9][0-9]{0,19})\Z")
_INSTANCE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")


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
        self._planned = entry, context.principal.binding_digest, context.session_id, context.turn_id, route
        return entry

    @staticmethod
    def _scope(observation):
        scopes = {(e.attrs.get("form_scope"), e.attrs.get("form_instance"))
                  for e in observation.elements if e.role in {"textbox", "button"}
                  and isinstance(e.attrs.get("form_scope"), str)
                  and e.attrs["form_scope"].startswith("dialog")}
        if len(scopes) != 1:
            raise BusinessToolRequestError("the current editor scope is ambiguous")
        scope, instance = next(iter(scopes))
        if not isinstance(instance, str) or not _INSTANCE.fullmatch(instance):
            raise BusinessToolRequestError("the current editor has no live instance")
        return scope, instance

    def accept_entry(self, entry, before, receipt, observation, *, context):
        planned, self._planned = self._planned, None
        if (planned is None or planned[:4] != (entry, context.principal.binding_digest,
                context.session_id, context.turn_id) or entry.control not in before.elements
                or not receipt.ok or receipt.action != "click" or receipt.navigated
                or not receipt.write_guard_held
                or receipt.args_echo != {"ref": entry.control.ref, "epoch": before.epoch}
                or observation.epoch <= before.epoch or observation.render_state != "ready"
                or observation.record_route != planned[4]
                or urlsplit(observation.url).path != planned[4] or urlsplit(observation.url).query):
            raise BusinessToolRequestError("the editor receipt did not bind this entry")
        scope, instance = self._scope(observation)
        record = _TARGET.fullmatch(entry.reference)[1]
        self._authorize(record, context)
        source = self.source.read(REVIEWED_FILES[1])
        if source is None:
            raise BusinessToolRequestError("the reviewed editor source is unavailable")
        self._current = NativeFormState(context.principal.actor_authority_digest,
            context.session_id, context.turn_id, instance, "update", record, planned[4], scope,
            REVIEWED_FILES[1], source.binding_digest, "客户编辑", "确 定")
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
            if (observation.render_state != "ready" or observation.record_route != state.route
                    or urlsplit(observation.url).path != state.route or urlsplit(observation.url).query
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


class NativeEntryFormPort(YudaoFormPort):
    def __init__(self, verifier):
        super().__init__(verifier.source, verifier)
        self.goal_source = verifier.source

    def click_policy(self, tabs):
        return NativeEntryClickPolicy(self._verifier, tabs)

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
                and observation.record_route == planned[4]
                and urlsplit(observation.url).path == planned[4]):
            return ""
        return self.tabs.decide(observation, element) if self.tabs else "only a reviewed current form entry is allowed"
