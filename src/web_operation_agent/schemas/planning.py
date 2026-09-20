# --------------------------------------------------------------------------------------
# Source evidence
# --------------------------------------------------------------------------------------
from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field

from .common import SCHEMA_VERSIONS, _require, _str, digest

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
