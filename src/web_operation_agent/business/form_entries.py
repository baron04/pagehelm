"""Current-page form entry candidates; private references never become model authority."""
from dataclasses import dataclass
import hashlib
import hmac
import json
import re
import secrets
from typing import Protocol, runtime_checkable

from .forms import ApplicationFormEntry, ApplicationFormEntryPort
from .errors import (BusinessToolContractError, BusinessToolIdentityError,
                     BusinessToolPermissionError, BusinessToolRequestError)
from .tools.contracts import form_control_unavailable
from ..state.identity import PrincipalError


@dataclass(frozen=True)
class FormEntryCandidate:
    reference: str
    label: str
    kind: str

    def __post_init__(self):
        if (not isinstance(self.reference, str) or not 1 <= len(self.reference) <= 300
                or not isinstance(self.label, str) or not self.label.strip() or len(self.label) > 120
                or not isinstance(self.kind, str) or self.kind not in {"create", "update"}):
            raise ValueError("form entry candidates require bounded references, labels and kinds")


@runtime_checkable
class FormEntryDiscoveryPort(ApplicationFormEntryPort, Protocol):
    def form_entries(self, observation, *, context) -> tuple[FormEntryCandidate, ...]:
        """Reviewed, actor-visible entries on this page; no permission to submit."""
        ...


FIND_FORM_ENTRIES_SCHEMA = {"type": "function", "function": {
    "name": "find_form_entries", "description": (
        "Find reviewed form openers on the current page for temporary prefill and manual "
        "handoff. Returns short-lived opaque entry IDs, not selectors or permission to "
        "submit. If none, discover/navigate to the relevant page first; absence here is "
        "not application-wide absence."),
    "parameters": {"type": "object", "additionalProperties": False, "properties": {}}}}
OPEN_FORM_ENTRY_SCHEMA = {"type": "function", "function": {
    "name": "open_form_entry", "description": (
        "Open one current reviewed entry returned by find_form_entries under the usual "
        "page approval and policy. Uses the application's exact control and verifies its "
        "receipt. Do not repeat an acknowledged or uncertain opening. After a pending "
        "render, observe/wait then describe_form; prepare/prefill never submits."),
    "parameters": {"type": "object", "additionalProperties": False,
        "properties": {"entry_id": {"type": "string", "pattern": "^form_entry_[0-9a-f]{32}$"}},
        "required": ["entry_id"]}}}
FORM_ENTRY_SCHEMAS = (FIND_FORM_ENTRIES_SCHEMA, OPEN_FORM_ENTRY_SCHEMA)


class FormEntryTools:
    def __init__(self, port, *, application_id, require_write_guard=False):
        if not isinstance(port, FormEntryDiscoveryPort):
            raise TypeError("form entry discovery needs a reviewed application port")
        self.port, self.application_id = port, application_id
        if not isinstance(require_write_guard, bool):
            raise TypeError("form entry guard requirement must be a boolean")
        self.require_write_guard = require_write_guard
        self.secret = secrets.token_bytes(32)
        self.candidates = {}
        self.pending = None
        self.opened = set()

    def _call(self, method, *args, **kwargs):
        try:
            return getattr(self.port, method)(*args, **kwargs)
        except PrincipalError:
            raise BusinessToolIdentityError("the form entry identity expired or changed") from None
        except (TypeError, ValueError) as exc:
            raise BusinessToolContractError(
                "the application returned an invalid form entry contract") from exc

    def _identity(self, context):
        if context is None:
            raise BusinessToolIdentityError("form entry requires a verified application actor")
        try:
            context.require_live(application_id=self.application_id)
        except PrincipalError:
            raise BusinessToolIdentityError("the form entry identity expired or changed") from None
        if not self.port.required_scopes <= set(context.principal.scopes):
            raise BusinessToolPermissionError("this actor cannot prepare application forms",
                                              layer="tool_authorization")
        return _binding_identity(context)

    def _binding(self, observation, context):
        return (*self._identity(context), observation.content_digest)

    def discover(self, observation, args, *, context):
        if not isinstance(args, dict) or args:
            raise BusinessToolRequestError("find_form_entries accepts no arguments")
        if self.pending is not None:
            raise BusinessToolRequestError("an entry was already opened; observe and describe_form first")
        self.candidates = {}
        binding = self._binding(observation, context)
        if observation.render_state != "ready":
            raise BusinessToolRequestError("form entry page is not ready")
        candidates = self._call("form_entries", observation, context=context)
        if (not isinstance(candidates, tuple) or len(candidates) > 8
                or any(not isinstance(c, FormEntryCandidate) for c in candidates)
                or len({c.reference for c in candidates}) != len(candidates)):
            raise BusinessToolContractError("the application returned invalid form entry candidates")
        rows = []
        for candidate in candidates:
            encoded = json.dumps((*binding, candidate.reference), ensure_ascii=False).encode()
            opaque = "form_entry_" + hmac.new(self.secret, encoded, hashlib.sha256).hexdigest()[:32]
            self.candidates[opaque] = (candidate, binding)
            rows.append({"entry_id": opaque, "label": candidate.label, "kind": candidate.kind})
        return {"entries": rows, "scope": "reviewed current-page entries for this actor only"}

    def plan(self, observation, args, *, context):
        if self.pending is not None:
            raise BusinessToolRequestError("do not repeat an acknowledged form opening")
        if (not isinstance(args, dict) or set(args) != {"entry_id"}
                or not isinstance(args["entry_id"], str)
                or not re.fullmatch(r"form_entry_[0-9a-f]{32}", args["entry_id"])):
            raise BusinessToolRequestError("open_form_entry requires one current opaque entry_id")
        chosen = self.candidates.pop(args["entry_id"], None)
        self.candidates = {}
        if chosen is None or chosen[1] != self._binding(observation, context):
            raise BusinessToolRequestError("the form entry binding is stale or belongs to another turn")
        if (*_binding_identity(context), chosen[0].reference) in self.opened:
            raise BusinessToolRequestError("this form entry was already acknowledged; do not open it again")
        entry = self._call("plan_entry", chosen[0].reference, observation, context=context)
        if (not isinstance(entry, ApplicationFormEntry) or entry.reference != chosen[0].reference
                or entry.control not in observation.elements or entry.control.epoch != observation.epoch
                or entry.control.role not in {"button", "link"}
                or form_control_unavailable(entry.control.attrs)):
            if isinstance(entry, ApplicationFormEntry):
                self._call("discard_entry", entry, context=context)
            raise BusinessToolContractError("the application did not issue an exact enabled form opener")
        return entry

    def acknowledged(self, entry, before, receipt, *, context):
        # This method receives only the actual dispatched receipt. A failed receipt
        # or failed verification cannot authorize retrying a possibly opened entry.
        self.opened.add((*_binding_identity(context), entry.reference))
        if (not receipt.ok or receipt.action != "click"
                or (self.require_write_guard and not receipt.write_guard_held)
                or receipt.args_echo != {"ref": entry.control.ref, "epoch": before.epoch}
                or receipt.observation is None or receipt.observation.epoch <= before.epoch):
            self._call("discard_entry", entry, context=context)
            raise BusinessToolRequestError("the form opener has no successful fresh guarded receipt")
        self.pending = (entry, before, receipt, _binding_identity(context), context)

    def accept_current(self, observation, *, context):
        if self.pending is None:
            return ""
        entry, before, receipt, identity, issued_context = self.pending
        try:
            if self._identity(context) != identity:
                raise BusinessToolIdentityError("the opened form actor or turn changed")
            if (observation.render_state == "pending"
                    or observation.epoch <= receipt.observation.epoch):
                raise _FormEntryPending("observe/wait for a fresh form before describing it")
            self.pending = None
            reference = self._call("accept_entry", entry, before, receipt, observation, context=context)
            if (not isinstance(reference, str) or not reference.strip()
                    or len(reference) > 300):
                raise BusinessToolContractError("the application did not bind the opened form")
        except _FormEntryPending:
            raise
        except BaseException as error:
            self.pending = None
            try:
                self._call("discard_entry", entry, context=issued_context)
            except Exception as cleanup:
                error.add_note("form entry release failed: " + type(cleanup).__name__)
            raise
        self._call("discard_entry", entry, context=issued_context)
        return reference

    def close(self):
        self.candidates = {}
        pending, self.pending = self.pending, None
        if pending is not None:
            entry, _, _, _, context = pending
            self._call("discard_entry", entry, context=context)

    def discard(self, entry, *, context):
        self._call("discard_entry", entry, context=context)


class _FormEntryPending(BusinessToolRequestError):
    """Keep the actual opener receipt while the same actor's page renders."""


def _binding_identity(context):
    return (context.principal.binding_digest, context.session_id, context.turn_id)
