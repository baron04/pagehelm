# --------------------------------------------------------------------------------------
# Remote runtime protocol
# --------------------------------------------------------------------------------------
from __future__ import annotations

import copy
from dataclasses import dataclass

from .browser import RECEIPT_ERRORS, ActionCommand, ActionReceipt, PageObservation
from .common import (SCHEMA_VERSIONS, _dict_copy, _non_negative_int, _page_origin,
                     _require, _schema_version, _sha256, _str, _strict_object,
                     _string_sequence, digest, parse_event_id)

@dataclass(frozen=True)
class ClientHello:
    """An embedded runtime reconnects to an existing server-owned session.

    ``last_event_id`` is the opaque SSE cursor last applied by the client. The server may
    replay later events, so receiving the same action event twice must never imply running
    the action twice; ``ActionEnvelope.idempotency_key`` defines that separate contract.
    """
    schema_version: int
    session_id: str
    client_id: str
    runtime_instance_id: str
    runtime: str
    capabilities: tuple[str, ...]
    page_origin: str
    application_id: str = ""
    principal_digest: str = ""
    last_event_id: str = ""

    def __post_init__(self) -> None:
        _schema_version(self.schema_version, "ClientHello")
        _str(self.session_id, "session_id")
        _str(self.client_id, "client_id")
        _str(self.runtime_instance_id, "runtime_instance_id")
        _str(self.runtime, "runtime")
        object.__setattr__(self, "capabilities",
                           _string_sequence(self.capabilities, "capabilities"))
        object.__setattr__(self, "page_origin", _page_origin(self.page_origin))
        _str(self.application_id, "application_id", allow_empty=True)
        _str(self.principal_digest, "principal_digest", allow_empty=True)
        _require(bool(self.application_id) == bool(self.principal_digest),
                 "application_id and principal_digest must be supplied together")
        if self.principal_digest:
            _sha256(self.principal_digest, "principal_digest")
        parse_event_id(self.last_event_id, name="last_event_id", allow_empty=True)

    def to_json(self) -> dict:
        return {"schema_version": self.schema_version, "session_id": self.session_id,
                "client_id": self.client_id, "runtime_instance_id": self.runtime_instance_id,
                "runtime": self.runtime,
                "capabilities": list(self.capabilities), "page_origin": self.page_origin,
                "application_id": self.application_id,
                "principal_digest": self.principal_digest,
                "last_event_id": self.last_event_id}

    @staticmethod
    def from_json(d: dict) -> "ClientHello":
        allowed = {"schema_version", "session_id", "client_id", "runtime_instance_id",
                   "runtime", "capabilities", "page_origin", "application_id",
                   "principal_digest", "last_event_id"}
        required = allowed - {"application_id", "principal_digest", "last_event_id"}
        d = _strict_object(d, "ClientHello", allowed=allowed, required=required)
        _require(isinstance(d["capabilities"], list), "capabilities must be an array")
        return ClientHello(schema_version=d["schema_version"], session_id=d["session_id"],
                           client_id=d["client_id"], runtime_instance_id=d["runtime_instance_id"],
                           runtime=d["runtime"],
                           capabilities=d["capabilities"],
                           page_origin=d["page_origin"],
                           application_id=d.get("application_id", ""),
                           principal_digest=d.get("principal_digest", ""),
                           last_event_id=d.get("last_event_id", ""))


@dataclass(frozen=True)
class ObservationEnvelope:
    """One sanitized runtime fact with a stable identity outside the page process."""
    schema_version: int
    session_id: str
    observation_id: str
    page_epoch: int
    observation: PageObservation
    events: tuple[dict, ...] = ()
    caused_by_action_id: str = ""

    def __post_init__(self) -> None:
        _schema_version(self.schema_version, "ObservationEnvelope")
        _str(self.session_id, "session_id")
        _str(self.observation_id, "observation_id")
        _non_negative_int(self.page_epoch, "page_epoch")
        _require(isinstance(self.observation, PageObservation),
                 "observation must be a PageObservation")
        _require(self.observation.schema_version == SCHEMA_VERSIONS["PageObservation"],
                 "embedded PageObservation schema_version mismatch")
        _require(self.observation.epoch == self.page_epoch,
                 "page_epoch must equal observation.epoch")
        object.__setattr__(self, "observation", copy.deepcopy(self.observation))
        _require(isinstance(self.events, (list, tuple)), "events must be an array")
        copied_events: list[dict] = []
        for event in self.events:
            copied_events.append(_dict_copy(event, "events entry"))
        object.__setattr__(self, "events", tuple(copied_events))
        _str(self.caused_by_action_id, "caused_by_action_id", allow_empty=True)

    def to_json(self) -> dict:
        return {"schema_version": self.schema_version, "session_id": self.session_id,
                "observation_id": self.observation_id, "page_epoch": self.page_epoch,
                "observation": self.observation.to_json(),
                "events": copy.deepcopy(list(self.events)),
                "caused_by_action_id": self.caused_by_action_id}

    @staticmethod
    def from_json(d: dict) -> "ObservationEnvelope":
        allowed = {"schema_version", "session_id", "observation_id", "page_epoch",
                   "observation", "events", "caused_by_action_id"}
        required = allowed - {"events", "caused_by_action_id"}
        d = _strict_object(d, "ObservationEnvelope", allowed=allowed, required=required)
        _require(isinstance(d.get("events", []), list), "events must be an array")
        return ObservationEnvelope(
            schema_version=d["schema_version"], session_id=d["session_id"],
            observation_id=d["observation_id"], page_epoch=d["page_epoch"],
            observation=PageObservation.from_json(d["observation"]),
            events=d.get("events", ()),
            caused_by_action_id=d.get("caused_by_action_id", ""),
        )

    @property
    def content_digest(self) -> str:
        return digest(self.to_json())


@dataclass(frozen=True)
class ActionEnvelope:
    """One atomic action based on one named observation.

    The pair ``(session_id, action_id)`` is the protocol idempotency key. A client keeps
    the first ``ActionResultEnvelope`` for that key and returns the cached bytes if an SSE
    reconnect delivers the action again. It must not execute the command a second time.
    ``page_epoch`` and ``based_on_observation_id`` let the client reject a command derived
    from stale page state before touching the page.
    """
    schema_version: int
    session_id: str
    turn_id: str
    action_id: str
    based_on_observation_id: str
    page_epoch: int
    deadline: int                  # absolute Unix time in milliseconds
    command_digest: str
    command: ActionCommand

    def __post_init__(self) -> None:
        _schema_version(self.schema_version, "ActionEnvelope")
        _str(self.session_id, "session_id")
        _str(self.turn_id, "turn_id")
        _str(self.action_id, "action_id")
        _str(self.based_on_observation_id, "based_on_observation_id")
        _non_negative_int(self.page_epoch, "page_epoch")
        _require(_non_negative_int(self.deadline, "deadline") > 0,
                 "deadline must be a positive Unix millisecond timestamp")
        _sha256(self.command_digest, "command_digest")
        _require(isinstance(self.command, ActionCommand), "command must be an ActionCommand")
        object.__setattr__(self, "command", copy.deepcopy(self.command))
        _require(self.command.command_id == self.action_id,
                 "command.command_id must equal action_id")
        _require(self.command_digest == self.command.content_digest,
                 "command_digest must match the canonical ActionCommand")

    @property
    def idempotency_key(self) -> tuple[str, str]:
        return self.session_id, self.action_id

    @property
    def content_digest(self) -> str:
        return digest(self.to_json())

    def to_json(self) -> dict:
        return {"schema_version": self.schema_version, "session_id": self.session_id,
                "turn_id": self.turn_id, "action_id": self.action_id,
                "based_on_observation_id": self.based_on_observation_id,
                "page_epoch": self.page_epoch, "deadline": self.deadline,
                "command_digest": self.command_digest,
                "command": self.command.to_json()}

    @staticmethod
    def from_json(d: dict) -> "ActionEnvelope":
        allowed = {"schema_version", "session_id", "turn_id", "action_id",
                   "based_on_observation_id", "page_epoch", "deadline", "command_digest",
                   "command"}
        d = _strict_object(d, "ActionEnvelope", allowed=allowed, required=allowed)
        return ActionEnvelope(
            schema_version=d["schema_version"], session_id=d["session_id"],
            turn_id=d["turn_id"], action_id=d["action_id"],
            based_on_observation_id=d["based_on_observation_id"],
            page_epoch=d["page_epoch"], deadline=d["deadline"],
            command_digest=d["command_digest"],
            command=ActionCommand.from_json(d["command"]),
        )


ACTION_RESULT_STATUSES = frozenset({"completed", "rejected", "indeterminate"})

# Envelope-level refusals describe why no definite browser receipt exists. Browser action
# failures remain in ``RECEIPT_ERRORS`` on a completed result.
ACTION_RESULT_ERRORS = frozenset({
    "", "stale_observation", "deadline_expired", "capability_refused",
    "runtime_replaced", "execution_state_unknown", "user_intervened",
})


@dataclass(frozen=True)
class ActionResultEnvelope:
    """The idempotently replayable result for one ``ActionEnvelope``.

    ``completed`` means the browser attempt reached a definite receipt; the receipt's
    ``ok`` field still says whether that attempt succeeded. ``rejected`` means the client
    did not execute stale, expired, or unsupported work. ``indeterminate`` means execution
    may have happened but no trustworthy receipt exists and the server must not retry.
    """
    schema_version: int
    session_id: str
    turn_id: str
    action_id: str
    based_on_observation_id: str
    page_epoch: int
    command_digest: str
    status: str
    observation_after: ObservationEnvelope | None
    receipt: ActionReceipt | None = None
    error_code: str = ""
    error_detail: str = ""

    def __post_init__(self) -> None:
        _schema_version(self.schema_version, "ActionResultEnvelope")
        _str(self.session_id, "session_id")
        _str(self.turn_id, "turn_id")
        _str(self.action_id, "action_id")
        _str(self.based_on_observation_id, "based_on_observation_id")
        _non_negative_int(self.page_epoch, "page_epoch")
        _sha256(self.command_digest, "command_digest")
        _str(self.status, "status")
        _require(self.status in ACTION_RESULT_STATUSES,
                 f"unknown action result status {self.status!r}")
        _str(self.error_code, "error_code", allow_empty=True)
        _str(self.error_detail, "error_detail", allow_empty=True)
        _require(self.error_code in ACTION_RESULT_ERRORS,
                 f"unknown action result error code {self.error_code!r}")

        if self.status == "completed":
            _require(isinstance(self.receipt, ActionReceipt),
                     "completed action result requires a receipt")
            _require(self.observation_after is not None,
                     "completed action result requires observation_after")
            _require(not self.error_code,
                     "completed action result uses receipt.error_code, not envelope.error_code")
            _require(not self.error_detail,
                     "completed action result uses receipt.error_detail, not envelope.error_detail")
        else:
            _require(self.receipt is None,
                     f"{self.status} action result must not claim a definite receipt")
            _require(bool(self.error_code),
                     f"{self.status} action result requires error_code")
            if self.status == "rejected":
                _require(self.observation_after is not None,
                         "rejected action result requires a fresh observation")

        if self.receipt is not None:
            object.__setattr__(self, "receipt", copy.deepcopy(self.receipt))
            _require(self.receipt.schema_version == SCHEMA_VERSIONS["ActionReceipt"],
                     "embedded ActionReceipt schema_version mismatch")
            _require(self.receipt.command_id == self.action_id,
                     "receipt.command_id must equal action_id")
            _require(self.receipt.error_code in RECEIPT_ERRORS,
                     f"unknown receipt error code {self.receipt.error_code!r}")
            _require(self.receipt.epoch_before == self.page_epoch,
                     "receipt.epoch_before must equal result page_epoch")
            _require(self.receipt.epoch_after > self.receipt.epoch_before,
                     "completed receipt must advance the observation epoch")
            _require(bool(self.receipt.url_before) and bool(self.receipt.url_after),
                     "completed receipt requires url_before and url_after")
            _require(self.receipt.navigated == (self.receipt.url_before != self.receipt.url_after),
                     "receipt.navigated must agree with its before/after URLs")
            if self.receipt.ok:
                _require(not self.receipt.error_code and not self.receipt.error_detail,
                         "successful receipt must not carry an error")
            else:
                _require(bool(self.receipt.error_code),
                         "failed receipt requires an error_code")

        if self.observation_after is not None:
            _require(isinstance(self.observation_after, ObservationEnvelope),
                     "observation_after must be an ObservationEnvelope")
            _require(self.observation_after.session_id == self.session_id,
                     "observation_after belongs to another session")
            _require(self.observation_after.caused_by_action_id == self.action_id,
                     "observation_after must name the action that caused it")
            _require(self.observation_after.page_epoch > self.page_epoch,
                     "observation_after must be fresher than the action's page_epoch")
            object.__setattr__(self, "observation_after",
                               copy.deepcopy(self.observation_after))

        if self.receipt is not None and self.observation_after is not None:
            _require(self.receipt.observation is not None,
                     "completed receipt must carry its fresh observation")
            _require(self.receipt.epoch_after == self.observation_after.page_epoch,
                     "receipt.epoch_after must equal observation_after.page_epoch")
            _require(self.receipt.observation.content_digest ==
                     self.observation_after.observation.content_digest,
                     "receipt and observation_after disagree about the page")
            _require(self.receipt.url_after == self.observation_after.observation.url,
                     "receipt.url_after must equal observation_after URL")

    @property
    def idempotency_key(self) -> tuple[str, str]:
        return self.session_id, self.action_id

    @property
    def content_digest(self) -> str:
        return digest(self.to_json())

    def to_json(self) -> dict:
        return {"schema_version": self.schema_version, "session_id": self.session_id,
                "turn_id": self.turn_id, "action_id": self.action_id,
                "based_on_observation_id": self.based_on_observation_id,
                "page_epoch": self.page_epoch, "command_digest": self.command_digest,
                "status": self.status,
                "observation_after": (self.observation_after.to_json()
                                      if self.observation_after else None),
                "receipt": self.receipt.to_json() if self.receipt else None,
                "error_code": self.error_code, "error_detail": self.error_detail}

    @staticmethod
    def from_json(d: dict) -> "ActionResultEnvelope":
        allowed = {"schema_version", "session_id", "turn_id", "action_id",
                   "based_on_observation_id", "page_epoch", "command_digest", "status",
                   "observation_after", "receipt", "error_code", "error_detail"}
        required = allowed - {"receipt", "error_code", "error_detail"}
        d = _strict_object(d, "ActionResultEnvelope", allowed=allowed, required=required)
        observation_after = d["observation_after"]
        receipt = d.get("receipt")
        return ActionResultEnvelope(
            schema_version=d["schema_version"], session_id=d["session_id"],
            turn_id=d["turn_id"], action_id=d["action_id"],
            based_on_observation_id=d["based_on_observation_id"],
            page_epoch=d["page_epoch"], command_digest=d["command_digest"],
            status=d["status"],
            observation_after=(ObservationEnvelope.from_json(observation_after)
                               if observation_after is not None else None),
            receipt=ActionReceipt.from_json(receipt) if receipt is not None else None,
            error_code=d.get("error_code", ""), error_detail=d.get("error_detail", ""),
        )


#: The kinds of confirmation a panel can be asked for; see `ApprovalRequest.kind`.
APPROVAL_KINDS = frozenset({"write", "navigation"})


@dataclass(frozen=True)
class ApprovalRequest:
    """User-visible description of one proposed effect; contains no hidden endpoint."""

    schema_version: int
    approval_id: str
    effect: str
    targets: tuple[str, ...]
    expires_at: int
    principal_digest: str = ""
    turn_id: str = ""
    operation_id: str = ""
    #: What the person is asked to confirm. ``write`` authorises one exact business change and
    #: can be declined; ``navigation`` asks before the assistant leaves the current page, whose
    #: unsaved input the application may save or discard, and offers only the confirmation --
    #: the person who does not want it stops the task instead.
    kind: str = "write"

    def __post_init__(self) -> None:
        _schema_version(self.schema_version, "ApprovalRequest")
        _require(self.kind in APPROVAL_KINDS,
                 f"kind must be one of {sorted(APPROVAL_KINDS)}")
        _str(self.approval_id, "approval_id")
        _str(self.effect, "effect")
        object.__setattr__(self, "targets", _string_sequence(self.targets, "targets"))
        _require(bool(self.targets), "targets must contain at least one user-visible target")
        _require(_non_negative_int(self.expires_at, "expires_at") > 0,
                 "expires_at must be a positive Unix timestamp in milliseconds")
        for name in ("principal_digest", "turn_id", "operation_id"):
            _str(getattr(self, name), name, allow_empty=True)
        bound = (bool(self.principal_digest), bool(self.turn_id), bool(self.operation_id))
        _require(len(set(bound)) == 1,
                 "principal_digest, turn_id, and operation_id must be supplied together")
        if self.principal_digest:
            _sha256(self.principal_digest, "principal_digest")

    def to_json(self) -> dict:
        return {"schema_version": self.schema_version, "approval_id": self.approval_id,
                "effect": self.effect, "targets": list(self.targets),
                "expires_at": self.expires_at,
                "principal_digest": self.principal_digest,
                "turn_id": self.turn_id,
                "operation_id": self.operation_id,
                "kind": self.kind}

    @staticmethod
    def from_json(d: dict) -> "ApprovalRequest":
        allowed = {"schema_version", "approval_id", "effect", "targets", "expires_at",
                   "principal_digest", "turn_id", "operation_id", "kind"}
        required = allowed - {"principal_digest", "turn_id", "operation_id", "kind"}
        d = _strict_object(d, "ApprovalRequest", allowed=allowed, required=required)
        _require(isinstance(d["targets"], list), "targets must be an array")
        return ApprovalRequest(schema_version=d["schema_version"],
                               approval_id=d["approval_id"], effect=d["effect"],
                               targets=d["targets"], expires_at=d["expires_at"],
                               principal_digest=d.get("principal_digest", ""),
                               turn_id=d.get("turn_id", ""),
                               operation_id=d.get("operation_id", ""),
                               kind=d.get("kind", "write"))

    @property
    def content_digest(self) -> str:
        return digest(self.to_json())


@dataclass(frozen=True)
class ApprovalDecision:
    """Authenticated response from the browser session that received the request."""

    schema_version: int
    session_id: str
    approval_id: str
    decision: str
    principal_digest: str = ""
    turn_id: str = ""
    operation_id: str = ""

    def __post_init__(self) -> None:
        _schema_version(self.schema_version, "ApprovalDecision")
        _str(self.session_id, "session_id")
        _str(self.approval_id, "approval_id")
        _require(self.decision in {"approved", "denied"},
                 "decision must be approved or denied")
        for name in ("principal_digest", "turn_id", "operation_id"):
            _str(getattr(self, name), name, allow_empty=True)
        bound = (bool(self.principal_digest), bool(self.turn_id), bool(self.operation_id))
        _require(len(set(bound)) == 1,
                 "principal_digest, turn_id, and operation_id must be supplied together")
        if self.principal_digest:
            _sha256(self.principal_digest, "principal_digest")

    def to_json(self) -> dict:
        return {"schema_version": self.schema_version, "session_id": self.session_id,
                "approval_id": self.approval_id, "decision": self.decision,
                "principal_digest": self.principal_digest,
                "turn_id": self.turn_id,
                "operation_id": self.operation_id}

    @staticmethod
    def from_json(d: dict) -> "ApprovalDecision":
        allowed = {"schema_version", "session_id", "approval_id", "decision",
                   "principal_digest", "turn_id", "operation_id"}
        required = allowed - {"principal_digest", "turn_id", "operation_id"}
        d = _strict_object(d, "ApprovalDecision", allowed=allowed, required=required)
        return ApprovalDecision(schema_version=d["schema_version"],
                                session_id=d["session_id"],
                                approval_id=d["approval_id"], decision=d["decision"],
                                principal_digest=d.get("principal_digest", ""),
                                turn_id=d.get("turn_id", ""),
                                operation_id=d.get("operation_id", ""))

    @property
    def content_digest(self) -> str:
        return digest(self.to_json())


@dataclass(frozen=True)
class CancellationRequest:
    """Authenticated request to stop one exact user message in the current session."""

    schema_version: int
    session_id: str
    message_id: str

    def __post_init__(self) -> None:
        _schema_version(self.schema_version, "CancellationRequest")
        _str(self.session_id, "session_id")
        _str(self.message_id, "message_id")
        _require(len(self.message_id) <= 200, "message_id exceeds 200 characters")

    def to_json(self) -> dict:
        return {"schema_version": self.schema_version, "session_id": self.session_id,
                "message_id": self.message_id}

    @staticmethod
    def from_json(d: dict) -> "CancellationRequest":
        allowed = {"schema_version", "session_id", "message_id"}
        d = _strict_object(d, "CancellationRequest", allowed=allowed, required=allowed)
        return CancellationRequest(**d)

    @property
    def content_digest(self) -> str:
        return digest(self.to_json())


TASK_EVENT_KINDS = frozenset({
    "session_started", "progress", "action", "approval_required", "terminal",
})


@dataclass(frozen=True)
class TaskEvent:
    """An ordered SSE event; ``event_id`` is the reconnect/resume cursor."""
    schema_version: int
    event_id: str
    session_id: str
    sequence: int
    kind: str
    turn_id: str
    payload: dict

    def __post_init__(self) -> None:
        _schema_version(self.schema_version, "TaskEvent")
        cursor_sequence = parse_event_id(self.event_id)
        _str(self.session_id, "session_id")
        _require(_non_negative_int(self.sequence, "sequence") >= 1,
                 "sequence must be at least 1")
        _require(cursor_sequence == self.sequence,
                 "event_id cursor must encode TaskEvent.sequence")
        _str(self.kind, "kind")
        _require(self.kind in TASK_EVENT_KINDS, f"unknown task event kind {self.kind!r}")
        _str(self.turn_id, "turn_id", allow_empty=True)
        object.__setattr__(self, "payload", _dict_copy(self.payload, "payload"))
        if self.kind == "action":
            action = ActionEnvelope.from_json(self.payload)
            _require(action.session_id == self.session_id,
                     "action event payload belongs to another session")
            _require(action.turn_id == self.turn_id,
                     "action event payload belongs to another turn")
        elif self.kind == "approval_required":
            ApprovalRequest.from_json(self.payload)

    def to_json(self) -> dict:
        return {"schema_version": self.schema_version, "event_id": self.event_id,
                "session_id": self.session_id, "sequence": self.sequence,
                "kind": self.kind, "turn_id": self.turn_id,
                "payload": copy.deepcopy(self.payload)}

    @staticmethod
    def from_json(d: dict) -> "TaskEvent":
        allowed = {"schema_version", "event_id", "session_id", "sequence", "kind",
                   "turn_id", "payload"}
        d = _strict_object(d, "TaskEvent", allowed=allowed, required=allowed)
        return TaskEvent(schema_version=d["schema_version"], event_id=d["event_id"],
                         session_id=d["session_id"], sequence=d["sequence"],
                         kind=d["kind"], turn_id=d["turn_id"], payload=d["payload"])

    @staticmethod
    def for_action(*, event_id: str, sequence: int,
                   action: ActionEnvelope) -> "TaskEvent":
        return TaskEvent(schema_version=SCHEMA_VERSIONS["TaskEvent"], event_id=event_id,
                         session_id=action.session_id, sequence=sequence, kind="action",
                         turn_id=action.turn_id, payload=action.to_json())

    def action(self) -> ActionEnvelope | None:
        return ActionEnvelope.from_json(self.payload) if self.kind == "action" else None
