"""Versioned wire schemas for the server agent and the thin browser runtime.

Every value that crosses a process boundary in this product is defined here, once. Producers,
runners and analysers must share one classification of terminal reasons, and an action name
must remain separate from the keys in its argument object. A single typed definition plus
`assert_terminal_reason_contract()`, which every consumer runs at import time, prevents those
forms of schema drift.

Nothing here knows about OFBiz, about a task id, or about an experiment arm.
"""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

SCHEMA_VERSIONS = {
    "EvidenceRef": 1,
    "SourceRelation": 1,
    "SourceMap": 1,
    "OperationContract": 1,
    "PageObservation": 1,
    "ActionCommand": 1,
    "ActionReceipt": 1,
    "ClientHello": 1,
    "ObservationEnvelope": 1,
    "ActionEnvelope": 1,
    "ActionResultEnvelope": 1,
    "TaskEvent": 1,
    "ApprovalRequest": 1,
    "ApprovalDecision": 1,
    "CancellationRequest": 1,
    "ControlPlan": 1,
}


class SchemaError(ValueError):
    """A value did not satisfy its schema. Always fatal: nothing is coerced."""


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode()).hexdigest()


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise SchemaError(msg)


def _str(v: Any, name: str, *, allow_empty: bool = False) -> str:
    _require(isinstance(v, str), f"{name} must be a string, got {type(v).__name__}")
    _require(allow_empty or v != "", f"{name} must not be empty")
    return v


def _non_negative_int(v: Any, name: str) -> int:
    _require(isinstance(v, int) and not isinstance(v, bool) and v >= 0,
             f"{name} must be a non-negative integer")
    return v


def _bool(v: Any, name: str) -> bool:
    _require(type(v) is bool, f"{name} must be a boolean")
    return v


def _dict_copy(v: Any, name: str) -> dict:
    _require(isinstance(v, dict), f"{name} must be an object")
    _require(all(isinstance(key, str) for key in v), f"{name} keys must be strings")
    return copy.deepcopy(v)


def _sha256(v: Any, name: str) -> str:
    value = _str(v, name)
    _require(bool(re.fullmatch(r"[0-9a-f]{64}", value)),
             f"{name} must be 64 lowercase hex chars")
    return value


def _schema_version(v: Any, schema: str) -> int:
    expected = SCHEMA_VERSIONS[schema]
    _require(type(v) is int and v == expected,
             f"{schema} schema_version must be {expected}")
    return v


def parse_event_id(value: Any, *, name: str = "event_id", allow_empty: bool = False) -> int:
    """Return the sequence encoded by the one canonical SSE cursor format."""
    cursor = _str(value, name, allow_empty=allow_empty)
    if cursor == "" and allow_empty:
        return 0
    match = re.fullmatch(r"e([0-9]{8})", cursor)
    _require(match is not None, f"{name} must use the e######## cursor format")
    sequence = int(match.group(1))
    _require(sequence >= 1, f"{name} sequence must be at least 1")
    return sequence


def _strict_object(d: Any, name: str, *, allowed: set[str], required: set[str]) -> dict:
    """Validate a public wire object without silently accepting protocol drift."""
    _require(isinstance(d, dict), f"{name} must be an object")
    extra = set(d) - allowed
    missing = required - set(d)
    _require(not extra, f"{name} has unknown fields {sorted(extra)}")
    _require(not missing, f"{name} is missing fields {sorted(missing)}")
    return d


def _string_sequence(v: Any, name: str) -> tuple[str, ...]:
    _require(isinstance(v, (list, tuple)), f"{name} must be an array")
    out = tuple(_str(item, f"{name} entry") for item in v)
    _require(len(out) == len(set(out)), f"{name} entries must be unique")
    return out


def _page_origin(v: Any) -> str:
    origin = _str(v, "page_origin")
    parsed = urlsplit(origin)
    _require(parsed.scheme in {"http", "https"} and bool(parsed.netloc),
             "page_origin must be an absolute http(s) origin")
    _require(parsed.path in {"", "/"} and not parsed.query and not parsed.fragment,
             "page_origin must not contain a path, query, or fragment")
    _require(parsed.username is None and parsed.password is None,
             "page_origin must not contain user information")
    return origin


# --------------------------------------------------------------------------------------
# Terminal reasons: ONE definition, shared by the runner that emits them and the analyser
# that scores them.
# --------------------------------------------------------------------------------------

#: Reasons a run can end with a business verdict that counts towards the result.
SCORABLE_TERMINAL_REASONS = frozenset({
    "agent_finished",         # the agent declared itself done, whatever the evaluator says
    "step_budget",            # the agent used its whole step budget
    "wall_clock_budget",      # the run hit its wall-clock ceiling
    "token_budget",           # the run hit its input-token ceiling
    "action_policy_budget",   # repeated pre-dispatch policy refusals exhausted recovery
    "agent_refused",          # the agent stopped and said it could not proceed
    "needs_input",            # the task paused for one bounded user clarification
    "no_progress",            # an unchanged decision repeated after one recovery prompt
    "planning_failed",        # the source arm could not produce a valid contract
    "approval_denied",        # the authenticated user declined the proposed effect
    "approval_expired",       # the visible confirmation elapsed before dispatch
    "user_cancelled",         # the authenticated user stopped the active task
    "business_mutation_failed",  # a known-safe server write refusal was returned
    "business_mutation_blocked",  # an in-flight/unresolved business key refused dispatch
})

#: Reasons that invalidate a run: apparatus, not behaviour. Never scored.
INVALIDATING_TERMINAL_REASONS = frozenset({
    "transport_error",
    "browser_crashed",
    "control_plane_error",
    "evaluator_error",
    "provider_error",
    "leak_detected",
    "approval_unavailable",   # the write-capable runner omitted its approval channel
    "approval_failed",        # the authenticated approval exchange failed before dispatch
})

#: A write may or may not have landed. Never scored, never retried.
INDETERMINATE_TERMINAL_REASONS = frozenset({
    "indeterminate_write",
})

TERMINAL_REASONS = (
    SCORABLE_TERMINAL_REASONS | INVALIDATING_TERMINAL_REASONS | INDETERMINATE_TERMINAL_REASONS
)


def assert_terminal_reason_contract(emitted: set[str], scorable: set[str]) -> None:
    """Every reason the runner can emit must be classified by the analyser.

    Adding an emitted reason without adding it to the shared classification would make a
    completed run uninterpretable. This assertion rejects that protocol mismatch immediately.
    """
    unclassified = emitted - TERMINAL_REASONS
    _require(not unclassified,
             f"runner can emit terminal reasons the analyser does not classify: {sorted(unclassified)}")
    unreachable = scorable - emitted
    _require(not unreachable,
             f"analyser scores terminal reasons the runner can never emit: {sorted(unreachable)}")


def classify_terminal(reason: str) -> str:
    """-> 'scorable' | 'invalid' | 'indeterminate'"""
    if reason in SCORABLE_TERMINAL_REASONS:
        return "scorable"
    if reason in INVALIDATING_TERMINAL_REASONS:
        return "invalid"
    if reason in INDETERMINATE_TERMINAL_REASONS:
        return "indeterminate"
    raise SchemaError(f"unknown terminal reason {reason!r}")


# --------------------------------------------------------------------------------------
# Source evidence
# --------------------------------------------------------------------------------------

@dataclass(frozen=True)
class EvidenceRef:
    """A citation into the deployed version's source tree.

    The release identity is carried on the reference itself rather than looked up later:
    a SourceMap compiled against one tree and replayed against another must fail loudly,
    not silently cite different bytes at the same line numbers.
    """
    repository_id: str
    release: str
    path: str
    start_line: int
    end_line: int
    sha256: str                      # over the exact cited byte range
    excerpt: str = ""

    def __post_init__(self) -> None:
        _str(self.repository_id, "repository_id")
        _str(self.release, "release")
        _str(self.path, "path")
        _require(not self.path.startswith("/") and ".." not in self.path.split("/"),
                 f"path must be repository-relative without traversal: {self.path!r}")
        _require(isinstance(self.start_line, int) and self.start_line >= 1,
                 "start_line must be a 1-based int")
        _require(isinstance(self.end_line, int) and self.end_line >= self.start_line,
                 "end_line must be >= start_line")
        _require(bool(re.fullmatch(r"[0-9a-f]{64}", self.sha256)), "sha256 must be 64 hex chars")

    @property
    def key(self) -> str:
        return f"{self.path}:{self.start_line}-{self.end_line}"

    def to_json(self) -> dict:
        return dataclasses.asdict(self)

    @staticmethod
    def from_json(d: dict) -> "EvidenceRef":
        _require(isinstance(d, dict), "EvidenceRef must be an object")
        allowed = {f.name for f in dataclasses.fields(EvidenceRef)}
        extra = set(d) - allowed
        _require(not extra, f"EvidenceRef has unknown fields {sorted(extra)}")
        return EvidenceRef(**{k: d[k] for k in allowed if k in d})


#: The relation vocabulary the compiler may emit. A compiler that wants to say something
#: outside this list must record it as `unsupported`, never invent a kind.
RELATION_KINDS = frozenset({
    "menu_link",              # a labelled navigation item points at a request
    "request_handler",       # a controller request is handled by a service / java event / view
    "request_response",      # a controller request's named response resolves to a view
    "view_screen",           # a controller view renders a named widget screen
    "form_target",           # a form declares which request it submits to
    "form_field",            # a form declares a submitted field
    "screen_includes_form",  # a screen renders a form
    "service_parameter",     # a service declares an IN/OUT parameter
    "service_entity",        # a service is declared against an entity
})


@dataclass(frozen=True)
class SourceRelation:
    kind: str
    subject: str
    object: str
    attrs: dict = field(default_factory=dict)
    evidence: tuple[EvidenceRef, ...] = ()

    def __post_init__(self) -> None:
        _require(self.kind in RELATION_KINDS,
                 f"unknown relation kind {self.kind!r}; supported: {sorted(RELATION_KINDS)}")
        _str(self.subject, "subject")
        _str(self.object, "object", allow_empty=True)
        _require(isinstance(self.attrs, dict), "attrs must be an object")
        _require(len(self.evidence) >= 1, f"relation {self.kind}/{self.subject} has no evidence")

    def to_json(self) -> dict:
        return {"kind": self.kind, "subject": self.subject, "object": self.object,
                "attrs": self.attrs, "evidence": [e.to_json() for e in self.evidence]}

    @staticmethod
    def from_json(d: dict) -> "SourceRelation":
        return SourceRelation(kind=d["kind"], subject=d["subject"], object=d["object"],
                              attrs=d.get("attrs", {}),
                              evidence=tuple(EvidenceRef.from_json(e) for e in d.get("evidence", [])))


@dataclass
class SourceMap:
    """Everything the planner is allowed to know about the deployment's source."""
    schema_version: int
    attestation_id: str
    repository_id: str
    release: str
    relations: list[SourceRelation]
    unsupported: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        _require(self.schema_version == SCHEMA_VERSIONS["SourceMap"],
                 f"SourceMap schema_version must be {SCHEMA_VERSIONS['SourceMap']}")
        _str(self.attestation_id, "attestation_id")

    def by_kind(self, kind: str) -> list[SourceRelation]:
        return [r for r in self.relations if r.kind == kind]

    def evidence_index(self) -> dict[str, EvidenceRef]:
        out: dict[str, EvidenceRef] = {}
        for r in self.relations:
            for e in r.evidence:
                out[e.key] = e
        return out

    def to_json(self) -> dict:
        return {"schema_version": self.schema_version, "attestation_id": self.attestation_id,
                "repository_id": self.repository_id, "release": self.release,
                "relations": [r.to_json() for r in self.relations],
                "unsupported": self.unsupported}

    @staticmethod
    def from_json(d: dict) -> "SourceMap":
        return SourceMap(schema_version=d["schema_version"], attestation_id=d["attestation_id"],
                         repository_id=d["repository_id"], release=d["release"],
                         relations=[SourceRelation.from_json(r) for r in d["relations"]],
                         unsupported=d.get("unsupported", []))

    @property
    def content_digest(self) -> str:
        return digest(self.to_json())


# --------------------------------------------------------------------------------------
# Operation contract
# --------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ContractStep:
    """One conditional move the source says is possible.

    `landmark` is what the page should show if the move succeeded; it is checked against
    the live observation, and a mismatch is a replan trigger rather than an error. This is
    architecture contract #4: source explains possibilities, the runtime decides facts.
    """
    intent: str
    webapp: str
    request: str
    landmark: str = ""
    note: str = ""
    citations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _str(self.intent, "step.intent")
        _str(self.webapp, "step.webapp")
        _str(self.request, "step.request", allow_empty=True)

    def to_json(self) -> dict:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class MutationSignature:
    """The write the source says this task most likely needs.

    It is a prediction to be checked, never an authorisation: the ControlPlan that decides
    what may actually be written is installed server-side and is not visible here.
    """
    webapp: str
    request: str
    required_params: tuple[str, ...] = ()
    service: str = ""
    citations: tuple[str, ...] = ()

    def to_json(self) -> dict:
        return dataclasses.asdict(self)


@dataclass
class OperationContract:
    schema_version: int
    task_ref: str
    summary: str
    entry_candidates: list[ContractStep] = field(default_factory=list)
    preconditions: list[str] = field(default_factory=list)
    steps: list[ContractStep] = field(default_factory=list)
    mutation: MutationSignature | None = None
    unknowns: list[str] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        _require(self.schema_version == SCHEMA_VERSIONS["OperationContract"],
                 f"OperationContract schema_version must be {SCHEMA_VERSIONS['OperationContract']}")
        _str(self.task_ref, "task_ref")
        _str(self.summary, "summary")

    def cited_keys(self) -> set[str]:
        keys = set(self.citations)
        for s in list(self.entry_candidates) + list(self.steps):
            keys |= set(s.citations)
        if self.mutation:
            keys |= set(self.mutation.citations)
        return keys

    def to_json(self) -> dict:
        return {"schema_version": self.schema_version, "task_ref": self.task_ref,
                "summary": self.summary,
                "entry_candidates": [s.to_json() for s in self.entry_candidates],
                "preconditions": list(self.preconditions),
                "steps": [s.to_json() for s in self.steps],
                "mutation": self.mutation.to_json() if self.mutation else None,
                "unknowns": list(self.unknowns), "citations": list(self.citations)}

    @staticmethod
    def from_json(d: dict) -> "OperationContract":
        _require(isinstance(d, dict), "OperationContract must be an object")
        allowed = {"schema_version", "task_ref", "summary", "entry_candidates", "preconditions",
                   "steps", "mutation", "unknowns", "citations"}
        extra = set(d) - allowed
        _require(not extra, f"OperationContract has unknown fields {sorted(extra)}")

        def _steps(key):
            out = []
            for s in d.get(key, []) or []:
                _require(isinstance(s, dict), f"{key} entries must be objects")
                unknown = set(s) - {"intent", "webapp", "request", "landmark", "note", "citations"}
                _require(not unknown, f"{key} entry has unknown fields {sorted(unknown)}")
                out.append(ContractStep(intent=s.get("intent", ""), webapp=s.get("webapp", ""),
                                        request=s.get("request", ""), landmark=s.get("landmark", ""),
                                        note=s.get("note", ""),
                                        citations=tuple(s.get("citations", []) or [])))
            return out

        m = d.get("mutation")
        mut = None
        if m:
            _require(isinstance(m, dict), "mutation must be an object")
            unknown = set(m) - {"webapp", "request", "required_params", "service", "citations"}
            _require(not unknown, f"mutation has unknown fields {sorted(unknown)}")
            mut = MutationSignature(webapp=_str(m.get("webapp", ""), "mutation.webapp"),
                                    request=_str(m.get("request", ""), "mutation.request"),
                                    required_params=tuple(m.get("required_params", []) or []),
                                    service=m.get("service", "") or "",
                                    citations=tuple(m.get("citations", []) or []))
        return OperationContract(
            schema_version=d.get("schema_version", SCHEMA_VERSIONS["OperationContract"]),
            task_ref=d["task_ref"], summary=d["summary"],
            entry_candidates=_steps("entry_candidates"), preconditions=list(d.get("preconditions", []) or []),
            steps=_steps("steps"), mutation=mut, unknowns=list(d.get("unknowns", []) or []),
            citations=list(d.get("citations", []) or []))

    @property
    def content_digest(self) -> str:
        return digest(self.to_json())


#: An arm that gets no source evidence still gets a contract-shaped object, so that the
#: two arms differ in content and not in code path.
def empty_contract(task_ref: str) -> OperationContract:
    return OperationContract(schema_version=SCHEMA_VERSIONS["OperationContract"],
                             task_ref=task_ref, summary="no source evidence is available",
                             unknowns=["every step of this task"])


# --------------------------------------------------------------------------------------
# Browser wire types
# --------------------------------------------------------------------------------------

#: The fixed, generic tool surface. The browser exposes exactly these and nothing else;
#: no task-specific or application-specific action ever appears here.
BROWSER_ACTIONS = frozenset({
    "observe", "goto", "click", "fill", "select", "submit", "back", "wait_for",
})


@dataclass(frozen=True)
class ActionCommand:
    command_id: str
    action: str
    args: dict = field(default_factory=dict)
    expect_navigation: bool = False

    def __post_init__(self) -> None:
        _str(self.command_id, "command_id")
        _str(self.action, "action")
        _require(self.action in BROWSER_ACTIONS,
                 f"unknown action {self.action!r}; the browser exposes {sorted(BROWSER_ACTIONS)}")
        object.__setattr__(self, "args", _dict_copy(self.args, "args"))
        _bool(self.expect_navigation, "expect_navigation")

    def to_json(self) -> dict:
        return {"schema_version": SCHEMA_VERSIONS["ActionCommand"], "command_id": self.command_id,
                "action": self.action, "args": copy.deepcopy(self.args),
                "expect_navigation": self.expect_navigation}

    @property
    def content_digest(self) -> str:
        return digest(self.to_json())

    @staticmethod
    def from_json(d: dict) -> "ActionCommand":
        allowed = {"schema_version", "command_id", "action", "args", "expect_navigation"}
        required = {"schema_version", "command_id", "action"}
        d = _strict_object(d, "ActionCommand", allowed=allowed, required=required)
        _schema_version(d["schema_version"], "ActionCommand")
        # Keep the action name in its dedicated envelope field. Arguments are untrusted
        # payload and must never select or overwrite the operation being dispatched.
        return ActionCommand(command_id=d["command_id"], action=d["action"],
                             args=d.get("args", {}),
                             expect_navigation=d.get("expect_navigation", False))


@dataclass(frozen=True)
class ElementRef:
    """A stable handle the model can name without seeing the DOM.

    `epoch` is the observation generation the ref was minted in. Using a ref from an older
    epoch is refused rather than resolved against a changed page.
    """
    ref: str
    role: str
    name: str
    tag: str
    epoch: int
    attrs: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        _str(self.ref, "ref")
        _str(self.role, "role")
        _str(self.name, "name", allow_empty=True)
        _str(self.tag, "tag")
        _non_negative_int(self.epoch, "epoch")
        object.__setattr__(self, "attrs", _dict_copy(self.attrs, "attrs"))

    def to_json(self) -> dict:
        return {"ref": self.ref, "role": self.role, "name": self.name, "tag": self.tag,
                "epoch": self.epoch, "attrs": copy.deepcopy(self.attrs)}

    @staticmethod
    def from_json(d: dict) -> "ElementRef":
        allowed = {"ref", "role", "name", "tag", "epoch", "attrs"}
        required = allowed - {"attrs"}
        d = _strict_object(d, "ElementRef", allowed=allowed, required=required)
        return ElementRef(ref=d["ref"], role=d["role"], name=d["name"], tag=d["tag"],
                          epoch=d["epoch"], attrs=d.get("attrs", {}))


@dataclass
class PageObservation:
    schema_version: int
    epoch: int
    url: str
    title: str
    landmarks: list[str]
    elements: list[ElementRef]
    # Optional text-DOM supplied by an embedded PageController.  The local Playwright
    # adapter keeps using structured elements only, so omitting this field preserves the
    # byte shape and digest of every historical observation.
    content: str = ""
    messages: list[str] = field(default_factory=list)
    truncated: bool = False

    def __post_init__(self) -> None:
        _schema_version(self.schema_version, "PageObservation")
        _non_negative_int(self.epoch, "epoch")
        _str(self.url, "url")
        _str(self.title, "title", allow_empty=True)
        _require(isinstance(self.landmarks, list), "landmarks must be an array")
        _require(all(isinstance(item, str) for item in self.landmarks),
                 "landmarks entries must be strings")
        _require(isinstance(self.elements, list), "elements must be an array")
        _require(all(isinstance(item, ElementRef) for item in self.elements),
                 "elements entries must be ElementRef values")
        _require(all(item.epoch == self.epoch for item in self.elements),
                 "every element epoch must equal the page epoch")
        refs = [item.ref for item in self.elements]
        _require(len(refs) == len(set(refs)), "element refs must be unique within an observation")
        _str(self.content, "content", allow_empty=True)
        _require(isinstance(self.messages, list), "messages must be an array")
        _require(all(isinstance(item, str) for item in self.messages),
                 "messages entries must be strings")
        _bool(self.truncated, "truncated")
        self.landmarks = copy.deepcopy(self.landmarks)
        self.elements = copy.deepcopy(self.elements)
        self.messages = copy.deepcopy(self.messages)

    def to_json(self) -> dict:
        out = {"schema_version": self.schema_version, "epoch": self.epoch, "url": self.url,
                "title": self.title, "landmarks": copy.deepcopy(self.landmarks),
                "elements": [e.to_json() for e in self.elements],
                "messages": copy.deepcopy(self.messages), "truncated": self.truncated}
        if self.content:
            out["content"] = self.content
        return out

    @staticmethod
    def from_json(d: dict) -> "PageObservation":
        allowed = {"schema_version", "epoch", "url", "title", "landmarks", "elements",
                   "content", "messages", "truncated"}
        required = {"schema_version", "epoch", "url", "title", "landmarks", "elements"}
        d = _strict_object(d, "PageObservation", allowed=allowed, required=required)
        _require(isinstance(d["landmarks"], list), "landmarks must be an array")
        _require(isinstance(d["elements"], list), "elements must be an array")
        _require(isinstance(d.get("messages", []), list), "messages must be an array")
        return PageObservation(schema_version=d["schema_version"], epoch=d["epoch"], url=d["url"],
                               title=d["title"], landmarks=copy.deepcopy(d["landmarks"]),
                               elements=[ElementRef.from_json(e) for e in d.get("elements", [])],
                               content=_str(d.get("content", ""), "content", allow_empty=True),
                               messages=copy.deepcopy(d.get("messages", [])),
                               truncated=d.get("truncated", False))

    @property
    def content_digest(self) -> str:
        return digest(self.to_json())


@dataclass
class ActionReceipt:
    """What the browser did, echoed back in full.

    `action` and `args_echo` are echoed rather than reconstructed so transport loss or renaming
    is caught directly by `tests/test_transport.py`.
    """
    schema_version: int
    command_id: str
    action: str
    args_echo: dict
    ok: bool
    error_code: str = ""
    error_detail: str = ""
    url_before: str = ""
    url_after: str = ""
    navigated: bool = False
    epoch_before: int = 0
    epoch_after: int = 0
    elapsed_ms: int = 0
    #: True when the browser re-authenticated during this action. OFBiz carries its session
    #: between webapps in a URL parameter, so any navigation that does not inherit it lands on
    #: a login form. Recovering is session lifecycle, which is the browser's job, but it is
    #: reported rather than hidden: it is applied identically for every arm and it must be
    #: visible when reading a trace.
    relogin: bool = False
    observation: PageObservation | None = None

    def __post_init__(self) -> None:
        _schema_version(self.schema_version, "ActionReceipt")
        _str(self.command_id, "command_id")
        _str(self.action, "action")
        _require(self.action in BROWSER_ACTIONS,
                 f"unknown receipt action {self.action!r}")
        self.args_echo = _dict_copy(self.args_echo, "args_echo")
        _bool(self.ok, "ok")
        _str(self.error_code, "error_code", allow_empty=True)
        _require(self.error_code in RECEIPT_ERRORS,
                 f"unknown receipt error code {self.error_code!r}")
        _str(self.error_detail, "error_detail", allow_empty=True)
        _str(self.url_before, "url_before", allow_empty=True)
        _str(self.url_after, "url_after", allow_empty=True)
        _bool(self.navigated, "navigated")
        _non_negative_int(self.epoch_before, "epoch_before")
        _non_negative_int(self.epoch_after, "epoch_after")
        _non_negative_int(self.elapsed_ms, "elapsed_ms")
        _bool(self.relogin, "relogin")
        _require(self.observation is None or isinstance(self.observation, PageObservation),
                 "observation must be a PageObservation or null")
        self.observation = copy.deepcopy(self.observation)

    def to_json(self) -> dict:
        return {"schema_version": self.schema_version, "command_id": self.command_id,
                "action": self.action, "args_echo": copy.deepcopy(self.args_echo), "ok": self.ok,
                "error_code": self.error_code, "error_detail": self.error_detail,
                "url_before": self.url_before, "url_after": self.url_after,
                "navigated": self.navigated, "epoch_before": self.epoch_before,
                "epoch_after": self.epoch_after, "elapsed_ms": self.elapsed_ms,
                "relogin": self.relogin,
                "observation": self.observation.to_json() if self.observation else None}

    @staticmethod
    def from_json(d: dict) -> "ActionReceipt":
        allowed = {"schema_version", "command_id", "action", "args_echo", "ok",
                   "error_code", "error_detail", "url_before", "url_after", "navigated",
                   "epoch_before", "epoch_after", "elapsed_ms", "relogin", "observation"}
        required = {"schema_version", "command_id", "action", "args_echo", "ok"}
        d = _strict_object(d, "ActionReceipt", allowed=allowed, required=required)
        obs = d.get("observation")
        return ActionReceipt(schema_version=d["schema_version"], command_id=d["command_id"],
                             action=d["action"], args_echo=d.get("args_echo", {}),
                             ok=d["ok"], error_code=d.get("error_code", ""),
                             error_detail=d.get("error_detail", ""),
                             url_before=d.get("url_before", ""), url_after=d.get("url_after", ""),
                             navigated=d.get("navigated", False),
                             epoch_before=d.get("epoch_before", 0),
                             epoch_after=d.get("epoch_after", 0),
                             elapsed_ms=d.get("elapsed_ms", 0),
                             relogin=d.get("relogin", False),
                             observation=PageObservation.from_json(obs)
                             if obs is not None else None)


#: Receipt error codes. Closed set, for the same reason terminal reasons are.
RECEIPT_ERRORS = frozenset({
    "", "unknown_ref", "stale_ref", "no_such_action", "bad_arguments", "element_not_actionable",
    "navigation_timeout", "origin_refused", "page_error", "write_refused",
    "deadline_expired", "stale_observation", "capability_refused", "runtime_replaced",
})


# --------------------------------------------------------------------------------------
# Remote runtime protocol
# --------------------------------------------------------------------------------------

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
        _page_origin(self.page_origin)
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
    "runtime_replaced", "execution_state_unknown",
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

    def __post_init__(self) -> None:
        _schema_version(self.schema_version, "ApprovalRequest")
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
                "operation_id": self.operation_id}

    @staticmethod
    def from_json(d: dict) -> "ApprovalRequest":
        allowed = {"schema_version", "approval_id", "effect", "targets", "expires_at",
                   "principal_digest", "turn_id", "operation_id"}
        required = allowed - {"principal_digest", "turn_id", "operation_id"}
        d = _strict_object(d, "ApprovalRequest", allowed=allowed, required=required)
        _require(isinstance(d["targets"], list), "targets must be an array")
        return ApprovalRequest(schema_version=d["schema_version"],
                               approval_id=d["approval_id"], effect=d["effect"],
                               targets=d["targets"], expires_at=d["expires_at"],
                               principal_digest=d.get("principal_digest", ""),
                               turn_id=d.get("turn_id", ""),
                               operation_id=d.get("operation_id", ""))

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


# --------------------------------------------------------------------------------------
# Control plane (server-side only; never serialised towards the browser or the model)
# --------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ControlTicket:
    webapp: str
    request: str
    targets: tuple[dict, ...]
    changes: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {"webapp": self.webapp, "request": self.request,
                "targets": [dict(t) for t in self.targets], "changes": dict(self.changes)}


@dataclass(frozen=True)
class ControlPlan:
    """What the user authorised for this run, derived from the public task spec.

    Held by the session, installed into the enforcing gateway, and never placed in a model
    prompt, a browser message or a contract. `redacted()` is what may appear in a ledger.
    """
    plan_id: str
    task_ref: str
    tickets: tuple[ControlTicket, ...]

    def to_json(self) -> dict:
        return {"plan_id": self.plan_id, "task_ref": self.task_ref,
                "tickets": [t.to_json() for t in self.tickets]}

    @property
    def content_digest(self) -> str:
        return digest(self.to_json())

    def redacted(self) -> dict:
        """Ledger-safe: proves a plan existed and was stable, reveals nothing about its content."""
        return {"plan_id": self.plan_id, "task_ref": self.task_ref,
                "ticket_count": len(self.tickets), "digest": self.content_digest}
