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
from ..answer.answering import StructuredQueryAnswer
from .read_summary import SummaryResult
from ..business.draft_steps import ApplicationDraftStepPort
from ..business.prefill_conversion import MutationPrefillPort


@dataclass(frozen=True, slots=True)
class DeliveryFeedback:
    """A bounded closing check; indices refer to this finish's deliverable items."""

    code: str
    message: str
    item_indices: tuple[int, ...] = ()

    def __post_init__(self):
        if (not isinstance(self.code, str) or not self.code or len(self.code) > 80
                or not self.code.isascii()
                or not self.code.replace("_", "").isalnum()
                or not isinstance(self.message, str) or not 1 <= len(self.message) <= 2000
                or not isinstance(self.item_indices, tuple)
                or len(self.item_indices) > 16 or len(set(self.item_indices)) != len(self.item_indices)
                or any(type(i) is not int or not 0 <= i < 16 for i in self.item_indices)):
            raise ValueError("invalid delivery feedback")


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
    mutation_prefill: MutationPrefillPort | None = None
    #: Optional closing reading of a finished answer against the turn's own sources,
    #: as (answer, sources) and the stated values the sources do not show. The loop
    #: sends the list back once and audits it. Unresolved item values mark those items
    #: and the turn incomplete; narrative-only values receive an explicit footer.
    untraced_numbers: Callable[[str, tuple[str, ...]], tuple[str, ...]] | None = None

    def __post_init__(self):
        names = [p.schema["function"]["name"] for p in self.procedures]
        if (not isinstance(self.instructions, str) or len(self.instructions) > 32000
                or not isinstance(self.procedures, tuple) or len(self.procedures) > 48
                or len(set(names)) != len(names) or "finish" in names
                or any(not callable(f) for f in (self.render_page, self.prepare, self.completion_conflict))):
            raise ValueError("invalid model loop profile")
        if self.untraced_numbers is not None and not callable(self.untraced_numbers):
            raise ValueError("model loop untraced numbers needs a callable reading")
        if self.draft_steps is not None and not isinstance(self.draft_steps, ApplicationDraftStepPort):
            raise ValueError("model loop draft steps need an application form port")
        if (self.mutation_prefill is not None
                and (not isinstance(self.mutation_prefill, MutationPrefillPort)
                     or not isinstance(self.mutation_prefill.required_scope, str)
                     or not self.mutation_prefill.required_scope)):
            raise ValueError("model loop mutation prefill needs a reviewed application port")


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


def normalize_table_payload(raw: object) -> dict | None:
    """Validate and normalize a bounded structured table payload (`columns`, `rows`, `total_rows`, optional `title`)."""
    if not isinstance(raw, dict):
        raise ValueError("structured table must be an object")
    raw_cols = raw.get("columns")
    raw_rows = raw.get("rows")
    if not isinstance(raw_cols, (list, tuple)) or not 1 <= len(raw_cols) <= 24:
        raise ValueError("structured table columns must have 1..24 headers")
    columns = [" ".join(str(col).split())[:80] for col in raw_cols]
    if any(not col for col in columns):
        raise ValueError("structured table column headers must be non-empty")
    if not isinstance(raw_rows, (list, tuple)) or len(raw_rows) > 2000:
        raise ValueError("structured table rows must be a bounded sequence")
    width = len(columns)
    rows: list[list[str]] = []
    for row in raw_rows:
        if not isinstance(row, (list, tuple)):
            raise ValueError("each structured table row must be a list or tuple")
        cells = [
            " ".join(str("" if idx >= len(row) or row[idx] is None else row[idx]).split())[:300]
            for idx in range(width)
        ]
        rows.append(cells)
    total_raw = raw.get("total_rows", len(rows))
    if type(total_raw) is not int or total_raw < len(rows):
        raise ValueError("structured table total_rows must be an integer >= len(rows)")
    out: dict = {"columns": columns, "rows": rows, "total_rows": total_raw}
    title = " ".join(str(raw.get("title") or "").split())[:120]
    if title:
        out["title"] = title
    return out


def normalize_input_options(raw: object) -> tuple[dict[str, str], ...]:
    """Validate and normalize 2..4 structured clarification options `{id?, label, prompt?}`."""
    if not isinstance(raw, (list, tuple)) or not 2 <= len(raw) <= 4:
        raise ValueError("input_options must contain 2..4 options")
    options: list[dict[str, str]] = []
    for idx, item in enumerate(raw, start=1):
        if isinstance(item, str):
            label = " ".join(item.split())
            if not 1 <= len(label) <= 200:
                raise ValueError("input_option label must be 1..200 chars")
            options.append({"id": f"opt-{idx}", "label": label})
            continue
        if not isinstance(item, dict) or set(item) - {"id", "label", "prompt"}:
            raise ValueError("each input_option must be an object with {id?, label, prompt?}")
        label = " ".join(str(item.get("label") or "").split())
        if not 1 <= len(label) <= 200:
            raise ValueError("input_option label must be 1..200 chars")
        opt_id = " ".join(str(item.get("id") or f"opt-{idx}").split())
        if not 1 <= len(opt_id) <= 60:
            raise ValueError("input_option id must be 1..60 chars")
        entry: dict[str, str] = {"id": opt_id, "label": label}
        if "prompt" in item and item["prompt"] is not None:
            if not isinstance(item["prompt"], str):
                raise ValueError("input_option prompt must be a string")
            prompt = item["prompt"].strip()
            if not 1 <= len(prompt) <= 2000:
                raise ValueError("input_option prompt must be 1..2000 chars")
            entry["prompt"] = prompt
        options.append(entry)
    return tuple(options)


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
    #: An explicitly requested, actor-read collection for exact server delivery.
    #: The model selects its turn-local reference; it never supplies these values.
    query_answer: StructuredQueryAnswer | SummaryResult | None = None
    #: A local action proof cannot settle an unresolved part of the user's request.
    #: Ask the existing completion reader before accepting a completed page handoff.
    review_completion: bool = False
    #: A server-authored explanation if the loop has stopped a page procedure
    #: as agent_refused. This field neither stops execution nor changes its reason.
    terminal_answer: str = ""
    #: Optional bounded structured table payload returned by a read procedure (ARC-06).
    structured_table: dict | None = None
    #: Optional 2..4 structured clarification choices for `needs_input` (UX-02).
    input_options: tuple[dict[str, str], ...] = ()

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
                                            or self.state != "completed")
                or self.query_answer is not None and (
                    not isinstance(self.query_answer, (StructuredQueryAnswer, SummaryResult))
                    or self.kind != "read" or self.state != "completed")
                or type(self.review_completion) is not bool
                or self.review_completion and (self.kind != "page" or self.state != "completed")
                or not isinstance(self.terminal_answer, str) or len(self.terminal_answer) > 4000
                or self.terminal_answer and (self.kind != "page"
                    or self.state not in {"refused", "needs_input", "unavailable", "unverified"}
                    or self.terminal_reason not in {"", "agent_refused"} or self.write_outcome is not None
                    or self.completion_command or self.review_completion)
                or self.structured_table is not None and (self.kind != "read" or self.state != "completed")
                or self.input_options and self.state != "needs_input"):
            raise ValueError("invalid model-loop result")
        if self.structured_table is not None:
            object.__setattr__(self, "structured_table", normalize_table_payload(self.structured_table))
        if self.input_options:
            object.__setattr__(self, "input_options", normalize_input_options(self.input_options))


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
