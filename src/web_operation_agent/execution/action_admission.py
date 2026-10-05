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


ADMITTED_ACTIONS = frozenset({"click", "fill", "select", "goto"})


class ActionEffect(StrEnum):
    NAVIGATION = "navigation"
    READ_ONLY = "read_only"
    TRANSIENT_DRAFT = "transient_draft"
    PERSISTENT = "persistent"
    UNKNOWN = "unknown"


class ActionAdmissionRefused(RuntimeError):
    """A typed, pre-dispatch refusal; adapters should not include private values."""

    def __init__(self, code: str):
        if code not in {"unreviewed", "invalid_target", "stale_binding", "unsafe_effect",
                        "already_consumed", "unverified", "policy_failed"}:
            raise ValueError("unknown action admission refusal")
        self.code = code
        super().__init__(code)


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
        if action == "click" and "expect_navigation" in args:
            if not isinstance(args["expect_navigation"], bool):
                raise ActionAdmissionRefused("invalid_target")
            expected |= {"expect_navigation"}
        if set(args) != expected or any(not isinstance(args[k], str) for k in expected - {"expect_navigation"}):
            raise ActionAdmissionRefused("invalid_target")
        if observation.render_state != "ready":
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
               *, context: OperationContext, checkpoint: Callable[[], None]) -> bool: ...


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

    def verify(self, intent, permit, receipt, *, context, checkpoint):
        self._check(context, checkpoint)
        before, after = intent.observation, receipt.observation
        echo = dict(receipt.args_echo)
        echoed_epoch = echo.pop("epoch", None)
        expected = intent.arguments
        expected.pop("expect_navigation", None)
        if (intent.operation_binding not in self._consumed
                or permit.binding_digest != intent.binding_digest
                or self._issued.get(intent.binding_digest) != permit
                or context.binding_digest != intent.operation_binding
                or receipt.action != intent.action or echo != expected or not receipt.ok
                or ("ref" in expected and echoed_epoch != before.epoch)
                or not receipt.write_guard_held or after is None
                or after.epoch <= before.epoch or after.render_state != "ready"
                or receipt.url_before != before.url or receipt.url_after != after.url
                or urlsplit(after.url)[:2] != urlsplit(before.url)[:2]):
            raise ActionAdmissionRefused("unverified")
        verified = self.policy.verify(permit, intent, receipt, context=context, checkpoint=checkpoint)
        self._check(context, checkpoint)
        if verified is not True:
            raise ActionAdmissionRefused("unverified")
