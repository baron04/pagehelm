"""Application-issued effects for existing browser tools, with one-use binding.

This is a server port, not a DOM classifier. An application must review its event
handlers and their effects; an ARIA role, source hit or HTTP write fence alone is
not such a review. The port grants neither persistent writes nor raw form drafts.
The existing approved FormPort owns transient business-value entry.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
from typing import Callable, Protocol, runtime_checkable
from urllib.parse import urljoin, urlsplit

from ..schemas import ActionReceipt, PageObservation
from ..state.identity import OperationContext
from ..transport.protocol import _remote_args_echo


ADMITTED_ACTIONS = frozenset({"click", "fill", "select", "goto"})


class ActionEffect(StrEnum):
    NAVIGATION = "navigation"
    READ_ONLY = "read_only"
    TRANSIENT_DRAFT = "transient_draft"
    PERSISTENT = "persistent"
    UNKNOWN = "unknown"


class ActionAdmissionRefused(RuntimeError):
    """A typed admission/result refusal; adapters must not include private values."""

    def __init__(self, code: str):
        if code not in {"unreviewed", "invalid_target", "stale_binding", "unsafe_effect",
                        "already_consumed", "unverified", "policy_failed"}:
            raise ValueError("unknown action admission refusal")
        self.code = code
        super().__init__(code)


class ActionResultPending(RuntimeError):
    """An acknowledged action is awaiting a fresh, guarded result observation."""


def _encoded(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class ActionIntent:
    """Immutable snapshots; each access gives an adapter a fresh, unshared copy."""

    action: str
    arguments_json: str
    observation_json: str
    operation_binding: str

    @classmethod
    def create(cls, action: str, arguments: dict, observation: PageObservation,
               context: OperationContext) -> "ActionIntent":
        if action not in ADMITTED_ACTIONS or not isinstance(arguments, dict):
            raise ActionAdmissionRefused("invalid_target")
        args = dict(arguments)
        if "epoch" in args:
            if type(args.pop("epoch")) is not int or arguments["epoch"] != observation.epoch:
                raise ActionAdmissionRefused("stale_binding")
        expected = {"url"} if action == "goto" else {"ref", "value"} if action in {"fill", "select"} else {"ref"}
        if action == "select" and "option_label" in args:
            if not isinstance(args["option_label"], str) or not args["option_label"].strip():
                raise ActionAdmissionRefused("invalid_target")
            expected |= {"option_label"}
        if action == "fill" and "commit" in args:
            if type(args["commit"]) is not bool:
                raise ActionAdmissionRefused("invalid_target")
            expected |= {"commit"}
        if action == "click" and "expect_navigation" in args:
            if not isinstance(args["expect_navigation"], bool):
                raise ActionAdmissionRefused("invalid_target")
            expected |= {"expect_navigation"}
        if set(args) != expected or any(not isinstance(args[k], str) for k in expected - {"expect_navigation", "commit"}):
            raise ActionAdmissionRefused("invalid_target")
        # A failed read can leave the current page blocked after an application
        # recovers. Navigating away is safe only when the application reviews the
        # exact destination; element actions still require a ready source page.
        if observation.render_state != "ready" and not (
                action == "goto" and observation.render_state == "blocked"):
            raise ActionAdmissionRefused("invalid_target")
        if action == "goto":
            here, there = urlsplit(observation.url), urlsplit(urljoin(observation.url, args["url"]))
            if (not args["url"] or there.scheme not in {"http", "https"}
                    or (here.scheme, here.netloc) != (there.scheme, there.netloc)):
                raise ActionAdmissionRefused("invalid_target")
        else:
            targets = [e for e in observation.elements if e.ref == args["ref"]]
            if len(targets) != 1 or targets[0].epoch != observation.epoch:
                raise ActionAdmissionRefused("invalid_target")
        try:
            return cls(action, _encoded(args), _encoded(observation.to_json()), context.binding_digest)
        except (TypeError, ValueError) as exc:
            raise ActionAdmissionRefused("invalid_target") from exc

    @property
    def arguments(self) -> dict:
        return json.loads(self.arguments_json)

    @property
    def observation(self) -> PageObservation:
        return PageObservation.from_json(json.loads(self.observation_json))

    @property
    def binding_digest(self) -> str:
        return _digest(_encoded([self.action, self.arguments_json, self.observation_json,
                                 self.operation_binding]))


@dataclass(frozen=True)
class ActionPermit:
    binding_digest: str
    effect: ActionEffect
    review: str
    postcondition: str

    def __post_init__(self):
        if (not isinstance(self.effect, ActionEffect)
                or not isinstance(self.binding_digest, str) or len(self.binding_digest) != 64
                or any(c not in "0123456789abcdef" for c in self.binding_digest)
                or any(not isinstance(v, str) or not v.strip() or len(v) > 200
                       for v in (self.review, self.postcondition))):
            raise ValueError("an action permit requires binding, effect, review and postcondition")


@runtime_checkable
class ApplicationActionPolicy(Protocol):
    application_id: str
    required_scope: str
    actions: frozenset[str]

    def admit(self, intent: ActionIntent, *, context: OperationContext,
              checkpoint: Callable[[], None]) -> ActionPermit: ...

    def verify(self, permit: ActionPermit, intent: ActionIntent, receipt: ActionReceipt,
               *, observation: PageObservation, context: OperationContext,
               checkpoint: Callable[[], None]) -> bool: ...


def verified_observation(intent, receipt, *, context, settlement=None, minimum_epoch=0):
    """Receipt/actor/page binding shared by read actions and transient form steps."""
    before, after = intent.observation, receipt.observation
    echo = dict(receipt.args_echo)
    echoed_epoch = echo.pop("epoch", None)
    expected = intent.arguments
    expected.pop("expect_navigation", None)
    expected = _remote_args_echo(expected)
    if (context.binding_digest != intent.operation_binding
            or receipt.action != intent.action or echo != expected or not receipt.ok
            or receipt.error_code
            or ("ref" in expected and echoed_epoch != before.epoch)
            or not receipt.write_guard_held or after is None
            or after.epoch <= before.epoch
            or receipt.url_before != before.url or receipt.url_after != after.url
            or urlsplit(after.url)[:2] != urlsplit(before.url)[:2]):
        raise ActionAdmissionRefused("unverified")
    if settlement is not None:
        fresh = settlement.observation
        if (settlement.action not in {"observe", "wait_for"} or not settlement.ok
                or settlement.error_code
                or not settlement.write_guard_held or fresh is None
                or fresh.epoch <= max(after.epoch, minimum_epoch)
                or settlement.url_after != fresh.url
                or urlsplit(settlement.url_before)[:2] != urlsplit(before.url)[:2]
                or urlsplit(fresh.url)[:2] != urlsplit(before.url)[:2]):
            raise ActionAdmissionRefused("unverified")
        after = fresh
    if after.render_state == "pending":
        raise ActionResultPending()
    if after.render_state != "ready":
        raise ActionAdmissionRefused("unverified")
    return after


class ActionAdmission:
    """Turn-local consumption. The application still owns effect and result proofs."""

    def __init__(self, policy: ApplicationActionPolicy, application_id: str):
        if (not isinstance(policy, ApplicationActionPolicy)
                or policy.application_id != application_id
                or not isinstance(policy.required_scope, str) or not policy.required_scope
                or not isinstance(policy.actions, frozenset)
                or not policy.actions or not policy.actions <= ADMITTED_ACTIONS):
            raise ValueError("action policy must declare this application's reviewed browser tools")
        self.policy = policy
        self.application_id = policy.application_id
        self.required_scope = policy.required_scope
        self.actions = policy.actions
        self._issued: dict[str, ActionPermit] = {}
        self._consumed: set[str] = set()
        self._verified_query = None
        self._verified_query_command = ""

    def query_verified(self, observation: PageObservation, *, command_id: str | None = None) -> bool:
        """A read snapshot counts only after this turn's guarded application check."""
        return (command_id is None or command_id == self._verified_query_command) and self._verified_query is not None and self._verified_query == (
            observation.url, observation.page_route, _encoded(observation.page_read),
            self._query_choices(observation))

    @staticmethod
    def _query_choices(observation):
        # Bind visible choices to the verified read. A later user edit or editor
        # replacement cannot inherit that proof even while its last GET is unchanged.
        return _encoded(sorted((e.attrs.get("query_field", ""), e.attrs.get("form_scope", ""),
            e.attrs.get("form_instance", ""), e.attrs.get("selected_option", ""), e.attrs.get("value", ""))
            for e in observation.elements if e.role in {"textbox", "combobox", "select", "spinbutton"}
            and e.attrs.get("page_control") == "query"))

    def _check(self, context, checkpoint):
        checkpoint()
        if not isinstance(context, OperationContext):
            raise ActionAdmissionRefused("stale_binding")
        context.require_live(application_id=self.application_id, required_scope=self.required_scope)

    def prepare(self, action, arguments, observation, *, context, checkpoint):
        self._check(context, checkpoint)
        if action not in self.actions:
            raise ActionAdmissionRefused("unreviewed")
        intent = ActionIntent.create(action, arguments, observation, context)
        permit = self.policy.admit(intent, context=context, checkpoint=checkpoint)
        self._check(context, checkpoint)
        if not isinstance(permit, ActionPermit) or permit.binding_digest != intent.binding_digest:
            raise ActionAdmissionRefused("stale_binding")
        if permit.effect not in {ActionEffect.NAVIGATION, ActionEffect.READ_ONLY}:
            raise ActionAdmissionRefused("unsafe_effect")
        self._issued[intent.binding_digest] = permit
        return intent, permit

    def consume(self, intent, permit, observation, *, context, checkpoint):
        self._check(context, checkpoint)
        current = ActionIntent.create(intent.action, intent.arguments, observation, context)
        if (current.binding_digest != permit.binding_digest
                or permit.binding_digest != intent.binding_digest
                or self._issued.get(intent.binding_digest) != permit):
            raise ActionAdmissionRefused("stale_binding")
        if intent.operation_binding in self._consumed:
            raise ActionAdmissionRefused("already_consumed")
        self._consumed.add(intent.operation_binding)

    def verify(self, intent, permit, receipt, *, context, checkpoint,
               settlement: ActionReceipt | None = None, minimum_epoch: int = 0):
        self._check(context, checkpoint)
        if (intent.operation_binding not in self._consumed
                or permit.binding_digest != intent.binding_digest
                or self._issued.get(intent.binding_digest) != permit):
            raise ActionAdmissionRefused("unverified")
        before = intent.observation
        after = verified_observation(intent, receipt, context=context,
                                     settlement=settlement, minimum_epoch=minimum_epoch)
        try:
            verified = self.policy.verify(permit, intent, ActionReceipt.from_json(receipt.to_json()),
                observation=PageObservation.from_json(after.to_json()),
                context=context, checkpoint=checkpoint)
        except ActionResultPending:
            self._check(context, checkpoint)
            raise
        self._check(context, checkpoint)
        if verified is not True:
            raise ActionAdmissionRefused("unverified")
        if (intent.action == "click" and permit.effect == ActionEffect.READ_ONLY
                and before.page_read is not None and after.page_read is not None
                and before.page_read["path"] == after.page_read["path"]
                and after.page_read["revision"] > before.page_read["revision"]):
            self._verified_query = (after.url, after.page_route, _encoded(after.page_read),
                                    self._query_choices(after))
            self._verified_query_command = receipt.command_id
