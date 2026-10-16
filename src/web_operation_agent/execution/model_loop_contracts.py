"""Immutable facts returned to the server's model-driven decision loop."""
from dataclasses import dataclass
import json
from urllib.parse import urlsplit
from typing import Callable, Literal, Protocol

from ..business.write_service import OperationOutcome
from ..business.tools.registry import _validate_schema
from ..goal.ports import OPTIONAL
from ..schemas import PageObservation, ActionReceipt
from ..state.identity import OperationContext
from ..business.draft_steps import ApplicationDraftStepPort


@dataclass(frozen=True, slots=True)
class PageActionOutcome:
    """A captured dispatch result; later observations cannot overwrite its diagnosis."""

    state: Literal["completed", "refused", "unavailable", "unverified"]
    dispatch: Literal["not_dispatched", "acknowledged", "uncertain"]
    error_code: str = ""
    receipt_json: str = ""
    observations_json: tuple[str, ...] = ()

    def __post_init__(self):
        if (self.state not in {"completed", "refused", "unavailable", "unverified"}
                or self.dispatch not in {"not_dispatched", "acknowledged", "uncertain"}
                or not isinstance(self.error_code, str) or len(self.error_code) > 100
                or not isinstance(self.observations_json, tuple) or len(self.observations_json) > 4):
            raise ValueError("invalid page action outcome")
        receipt = self.receipt
        if ((receipt is not None) != (self.dispatch == "acknowledged")
                or self.state == "completed" and (receipt is None or not receipt.ok)
                or self.state == "refused" and self.dispatch == "uncertain"
                or self.state == "refused" and self.dispatch == "acknowledged" and
                    (receipt is None or not receipt.no_effect or receipt.error_code != "option_unavailable")):
            raise ValueError("inconsistent page action outcome")
        for observation in self.observations_json:
            PageObservation.from_json(json.loads(observation))

    @property
    def receipt(self) -> ActionReceipt | None:
        return ActionReceipt.from_json(json.loads(self.receipt_json)) if self.receipt_json else None

    @property
    def observations(self) -> tuple[PageObservation, ...]:
        return tuple(PageObservation.from_json(json.loads(item)) for item in self.observations_json)


class ProcedurePage(Protocol):
    """The only browser capability an application procedure receives."""

    def observation(self) -> PageObservation: ...
    def execute(self, action: str, arguments: dict, *, navigating: bool = False) -> PageActionOutcome: ...
    def checkpoint(self) -> None: ...
    def stopped(self) -> bool: ...
    def action_observations(self) -> tuple[PageObservation, ...]: ...
    def handoff(self, fields: list[dict], submit: dict) -> PageActionOutcome: ...


@dataclass(frozen=True, slots=True)
class PageProcedureContext:
    operation: OperationContext
    page: ProcedurePage
    progress: Callable[[dict], None]
    audit: Callable[..., None]
    measure: Callable

    @property
    def obs(self):
        # PageObservation contains containers; hand over a separate immutable
        # snapshot by value on every access, never the executor's owned instance.
        return PageObservation.from_json(self.page.observation().to_json())

    @property
    def terminal_reason(self):
        return "stopped" if self.page.stopped() else ""


class ProcedureHandler(Protocol):
    def execute(self, arguments: dict, *, context: PageProcedureContext) -> "LoopToolResult": ...


@dataclass(frozen=True, slots=True)
class LoopProcedure:
    schema_json: str
    handler: ProcedureHandler
    kind: Literal["metadata", "read", "page"]
    scope: str
    required_capabilities: frozenset[str] = frozenset()
    requirements: Callable | None = None

    def __post_init__(self):
        schema = json.loads(self.schema_json)
        if (set(schema) != {"type", "function"} or schema["type"] != "function"
                or self.kind not in {"metadata", "read", "page"}
                or not isinstance(self.scope, str) or not self.scope or len(self.scope) > 100
                or not isinstance(self.required_capabilities, frozenset)
                or not self.required_capabilities <= OPTIONAL
                or not callable(getattr(self.handler, "execute", None))):
            raise ValueError("invalid application loop procedure")
        _validate_schema(schema["function"]["parameters"])

    @property
    def schema(self):
        return json.loads(self.schema_json)


@dataclass(frozen=True, slots=True)
class ModelLoopProfile:
    """Application knowledge and bounded procedures; no runtime or session state."""

    instructions: str
    procedures: tuple[LoopProcedure, ...]
    render_page: Callable[[PageObservation, str], str]
    prepare: Callable[[str, PageProcedureContext], str]
    completion_conflict: Callable[[PageObservation, str], tuple[str, ...]] = lambda _obs, _answer: ()
    verification_messages: Callable | None = None
    draft_steps: ApplicationDraftStepPort | None = None

    def __post_init__(self):
        names = [p.schema["function"]["name"] for p in self.procedures]
        if (not isinstance(self.instructions, str) or len(self.instructions) > 32000
                or not isinstance(self.procedures, tuple) or len(self.procedures) > 48
                or len(set(names)) != len(names) or "finish" in names
                or any(not callable(f) for f in (self.render_page, self.prepare, self.completion_conflict))):
            raise ValueError("invalid model loop profile")
        if self.draft_steps is not None and not isinstance(self.draft_steps, ApplicationDraftStepPort):
            raise ValueError("model loop draft steps need an application form port")


@dataclass(frozen=True, slots=True)
class AnswerTarget:
    """One actor-read application address that a later answer may cite as a link."""

    kind: Literal["page", "record"]
    address: str
    label: str
    identity: str

    def __post_init__(self):
        if not isinstance(self.address, str):
            raise ValueError("invalid answer navigation target")
        parsed = urlsplit(self.address)
        if (self.kind not in {"page", "record"}
                or not isinstance(self.address, str) or not 1 <= len(self.address) <= 1000
                or not self.address.startswith("/") or self.address.startswith("//")
                or parsed.scheme or parsed.netloc or parsed.query or parsed.fragment
                or any(ord(char) < 33 or ord(char) == 127 or char == "\\" for char in self.address)
                or not isinstance(self.label, str) or not 1 <= len(self.label) <= 160
                or not self.label.strip() or any(ord(char) < 32 or ord(char) == 127 for char in self.label)
                or not isinstance(self.identity, str) or not 1 <= len(self.identity) <= 160
                or any(ord(char) < 33 or ord(char) == 127 for char in self.identity)):
            raise ValueError("invalid answer navigation target")


@dataclass(frozen=True, slots=True)
class LoopToolResult:
    feedback: str
    kind: Literal["metadata", "read", "page", "write"] = "metadata"
    state: Literal["completed", "refused", "needs_input", "unavailable", "unverified", "indeterminate"] = "completed"
    dispatch: Literal["not_dispatched", "acknowledged", "uncertain"] = "not_dispatched"
    error_code: str = ""
    terminal_reason: str = ""
    write_outcome: OperationOutcome | None = None
    entered: tuple[tuple[str, str], ...] = ()
    trace: tuple[str, ...] = ()
    read_summary: tuple[str, str] | None = None
    read_key: str = ""
    read_count: int | None = None
    marked: str = ""
    completion_command: str = ""
    answer_targets: tuple[AnswerTarget, ...] = ()

    def __post_init__(self):
        if (not isinstance(self.feedback, str) or len(self.feedback) > 32000
                or self.kind not in {"metadata", "read", "page", "write"}
                or self.state not in {"completed", "refused", "needs_input", "unavailable", "unverified", "indeterminate"}
                or self.dispatch not in {"not_dispatched", "acknowledged", "uncertain"}
                or self.write_outcome is not None and self.kind != "write"
                or any(not isinstance(v, str) or len(v) > 100 for v in (self.error_code, self.terminal_reason))
                or self.read_summary is not None and (not isinstance(self.read_summary, tuple)
                    or len(self.read_summary) != 2 or any(not isinstance(v, str) or len(v) > 4000
                                                        for v in self.read_summary))
                or not isinstance(self.read_key, str) or len(self.read_key) > 200
                or self.read_count is not None and (type(self.read_count) is not int or self.read_count < 0
                    or not self.read_key or self.kind != "read")
                or not isinstance(self.entered, tuple) or len(self.entered) > 32
                or any(not isinstance(pair, tuple) or len(pair) != 2
                       or any(not isinstance(v, str) or len(v) > 4000 for v in pair) for pair in self.entered)
                or not isinstance(self.trace, tuple) or len(self.trace) > 40
                or any(not isinstance(v, str) or len(v) > 4000 for v in self.trace)
                or not isinstance(self.marked, str) or len(self.marked) > 200
                or not isinstance(self.completion_command, str) or len(self.completion_command) > 200
                or not isinstance(self.answer_targets, tuple) or len(self.answer_targets) > 8
                or any(not isinstance(target, AnswerTarget) for target in self.answer_targets)
                or self.answer_targets and (self.kind not in {"read", "metadata"}
                                            or self.state != "completed")):
            raise ValueError("invalid model-loop result")


@dataclass(frozen=True, slots=True)
class VerificationResult:
    state: Literal["passed", "disputed", "unavailable"]
    problems: tuple[str, ...] = ()

    def __post_init__(self):
        if (self.state not in {"passed", "disputed", "unavailable"}
                or not isinstance(self.problems, tuple)
                or len(self.problems) > 4
                or any(not isinstance(p, str) or len(p) > 200 for p in self.problems)
                or (self.state == "disputed") != bool(self.problems)):
            raise ValueError("invalid completion verification")
