"""Application-neutral Business Skills and typed server-tool dispatch.

The server Agent owns this catalog.  A Skill is a bounded playbook, while the executor
supplies current application facts.  Tool schemas become visible only after the Agent loads
the matching Skill, which makes ``load_skill -> tool -> ToolResult`` an enforced transition
instead of a sentence in a prompt.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from .identity import OperationContext, PrincipalError
from .mutation import (MemoryMutationJournal, MutationJournal,
                       MutationJournalError)
from .schemas import BROWSER_ACTIONS


_SKILL_ID = re.compile(r"^[a-z][a-z0-9_.-]{2,79}$")
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_SENSITIVE_KEY = re.compile(
    r"(?:password|passwd|pwd|secret|token|api[_-]?key|authorization)", re.I)
_MAX_RESULT_BYTES = 64 * 1024
BUSINESS_SKILL_MATCHER_VERSION = "business-skill-trigger-v2"
_EXPLICIT_TOOL_REFERENCE = re.compile(
    r"\bcall\s+`?([a-z][a-z0-9_]{1,63})`?", re.I)


def _trigger_tokens(value: str) -> tuple[str, ...]:
    """Tokenize Latin words and individual Han characters for bounded phrase matching."""
    return tuple(re.findall(r"[a-z0-9]+|[\u3400-\u4dbf\u4e00-\u9fff]", value.casefold()))


def _trigger_matches(trigger: str, task: str, *, max_gap_tokens: int = 3) -> bool:
    """Match a Skill phrase with word boundaries and a small amount of natural-language fill."""
    wanted = _trigger_tokens(trigger)
    available = _trigger_tokens(task)
    if not wanted or not available:
        return False
    cursor = -1
    for position, token in enumerate(wanted):
        # A phrase can occur anywhere in the task. Only the distance between its own words
        # is bounded; otherwise a one-word domain trigger would inexplicably stop matching
        # near the end of an ordinary sentence.
        upper = (len(available) if position == 0
                 else min(len(available), cursor + max_gap_tokens + 2))
        try:
            cursor = available.index(token, cursor + 1, upper)
        except ValueError:
            return False
    return True


class BusinessToolError(RuntimeError):
    """A safe, expected refusal or read failure that the Agent may reason about."""

    code = "business_tool"


class BusinessToolRequestError(BusinessToolError):
    """The requested tool call is invalid or is not permitted for this turn."""

    code = "business_request"


class BusinessToolIdentityError(BusinessToolError):
    """The application credential or bound principal cannot authorize the call."""

    code = "business_identity"


class BusinessToolAuthenticationError(BusinessToolIdentityError):
    """The delegated application credential is missing, expired, or rejected."""

    code = "business_authentication"


class BusinessToolUnavailableError(BusinessToolError):
    """A valid read could not reach the application service."""

    code = "business_unavailable"


class BusinessToolContractError(BusinessToolError):
    """The application integration returned data outside its declared contract."""

    code = "business_contract"


class BusinessMutationIndeterminate(RuntimeError):
    """A dispatched mutation may have landed and therefore must never be retried."""


def _string_list(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value \
            or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{name} must be a non-empty list of strings")
    return tuple(item.strip() for item in value)


def _has_sensitive_key(value: object) -> bool:
    if isinstance(value, dict):
        return any(_SENSITIVE_KEY.search(str(key)) or _has_sensitive_key(item)
                   for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_has_sensitive_key(item) for item in value)
    return False


@dataclass(frozen=True)
class BusinessToolResult:
    """Bounded runtime facts for the model plus a smaller audit projection."""

    content: dict
    audit: dict
    allowed_routes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.content, dict) or not isinstance(self.audit, dict):
            raise ValueError("business tool content and audit must be JSON objects")
        if _has_sensitive_key(self.content) or _has_sensitive_key(self.audit):
            raise ValueError("business tool results may not contain credential-shaped keys")
        try:
            encoded = json.dumps(
                self.content, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False).encode("utf-8")
            json.dumps(self.audit, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("business tool results must be finite JSON") from exc
        if len(encoded) > _MAX_RESULT_BYTES:
            raise ValueError("business tool result exceeds 64 KiB")
        if not isinstance(self.allowed_routes, tuple) \
                or len(self.allowed_routes) != len(set(self.allowed_routes)):
            raise ValueError("business tool routes must be a unique tuple")
        for route in self.allowed_routes:
            parsed = urlsplit(route) if isinstance(route, str) else None
            safe = bool(
                parsed and route.startswith("/") and not route.startswith("//")
                and not parsed.scheme and not parsed.netloc and not parsed.fragment
                and len(route) <= 1000 and "%" not in parsed.path
                and not re.search(r"[\\\x00-\x20\x7f]", route)
                and ".." not in parsed.path.split("/"))
            if not safe:
                raise ValueError("business tool routes must be safe relative paths")
            if not _contains_exact_string(self.content, route):
                raise ValueError("each business tool route must appear in its content")

    @property
    def content_digest(self) -> str:
        raw = json.dumps(self.content, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def render(self) -> str:
        return json.dumps(self.content, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)


def _contains_exact_string(value: object, target: str) -> bool:
    if isinstance(value, str):
        return value == target
    if isinstance(value, dict):
        return any(_contains_exact_string(item, target) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_exact_string(item, target) for item in value)
    return False


class BusinessToolExecutor(Protocol):
    def tool_schemas(self) -> tuple[dict, ...]: ...

    def tool_effects(self) -> dict[str, str]: ...

    def execute(self, name: str, args: dict, *,
                context: OperationContext | None = None) -> BusinessToolResult: ...

    def prepare_mutation(self, name: str, args: dict, *,
                         context: OperationContext | None = None
                         ) -> "BusinessMutationSpec": ...

    def execute_mutation(self, name: str, args: dict, *,
                         context: OperationContext | None = None
                         ) -> BusinessToolResult: ...


@dataclass(frozen=True)
class BusinessMutationSpec:
    """Executor-normalized effect shown to the user before any write is dispatched."""

    args: dict
    effect: str
    targets: tuple[str, ...]

    def __post_init__(self) -> None:
        if (not isinstance(self.args, dict)
                or any(not isinstance(key, str) for key in self.args)
                or _has_sensitive_key(self.args)):
            raise ValueError("mutation arguments must be a credential-free JSON object")
        if (not isinstance(self.effect, str) or not self.effect.strip()
                or len(self.effect.strip()) > 300):
            raise ValueError("mutation effect must be a non-empty string")
        if (not isinstance(self.targets, tuple) or not self.targets or len(self.targets) > 20
                or any(not isinstance(item, str) or not item.strip()
                       or len(item.strip()) > 300
                       for item in self.targets)):
            raise ValueError("mutation targets must be a non-empty tuple of strings")
        try:
            encoded = json.dumps(self.args, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("mutation arguments must be finite JSON") from exc
        if len(encoded.encode("utf-8")) > _MAX_RESULT_BYTES:
            raise ValueError("mutation arguments exceed 64 KiB")
        object.__setattr__(self, "args", json.loads(encoded))
        object.__setattr__(self, "effect", self.effect.strip())
        object.__setattr__(self, "targets", tuple(item.strip() for item in self.targets))


@dataclass(frozen=True)
class BusinessMutationProposal:
    """A single-use, digest-bound server mutation proposal."""

    tool: str
    args: dict
    effect: str
    targets: tuple[str, ...]
    nonce: str
    operation_context_digest: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.tool, str) or not _TOOL_NAME.fullmatch(self.tool):
            raise ValueError("mutation tool is invalid")
        validated = BusinessMutationSpec(self.args, self.effect, self.targets)
        object.__setattr__(self, "args", validated.args)
        object.__setattr__(self, "effect", validated.effect)
        object.__setattr__(self, "targets", validated.targets)
        if not isinstance(self.nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", self.nonce):
            raise ValueError("mutation nonce is invalid")
        if self.operation_context_digest and not re.fullmatch(
                r"[0-9a-f]{64}", self.operation_context_digest):
            raise ValueError("mutation operation context digest is invalid")

    @property
    def content_digest(self) -> str:
        raw = json.dumps({
            "schema_version": 1,
            "tool": self.tool,
            "args": self.args,
            "effect": self.effect,
            "targets": list(self.targets),
            "nonce": self.nonce,
            "operation_context_digest": self.operation_context_digest,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def redacted(self) -> dict:
        return {
            "schema_version": 1,
            "tool": self.tool,
            "argument_names": sorted(self.args),
            "target_count": len(self.targets),
            "digest": self.content_digest,
            "operation_context_digest": self.operation_context_digest,
        }


@dataclass(frozen=True)
class BusinessSkill:
    skill_id: str
    title: str
    intent: str
    triggers: tuple[str, ...]
    instructions: tuple[str, ...]
    tool_names: tuple[str, ...]
    provenance: tuple[str, ...]
    read_only: bool
    artifact_path: str
    artifact_sha256: str

    @classmethod
    def load(cls, path: str | Path) -> "BusinessSkill":
        artifact = Path(path)
        return cls.from_bytes(artifact.read_bytes(), artifact_uri=str(artifact))

    @classmethod
    def from_bytes(cls, raw: bytes, *, artifact_uri: str) -> "BusinessSkill":
        """Load an immutable Skill artifact without requiring a filesystem path.

        ``artifact_uri`` is a logical identity retained for diagnostics and provenance;
        callers may use schemes such as ``package://`` or ``memory://``.  The bytes are
        parsed immediately, so no temporary resource path needs to outlive this call.
        """
        if not isinstance(raw, bytes):
            raise TypeError("Business Skill content must be bytes")
        if (not isinstance(artifact_uri, str) or not artifact_uri.strip()
                or len(artifact_uri) > 2_000
                or re.search(r"[\x00-\x1f\x7f]", artifact_uri)):
            raise ValueError("Business Skill artifact_uri is invalid")
        artifact_uri = artifact_uri.strip()
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"invalid Business Skill JSON: {artifact_uri}") from exc
        required = {
            "schema_version", "skill_id", "title", "intent", "triggers",
            "instructions", "tool_names", "provenance", "read_only",
        }
        if not isinstance(data, dict) or set(data) != required or data.get("schema_version") != 1:
            raise ValueError("Business Skill fields or schema_version do not match schema 1")
        skill_id = data.get("skill_id")
        if not isinstance(skill_id, str) or not _SKILL_ID.fullmatch(skill_id):
            raise ValueError("skill_id is invalid")
        for name in ("title", "intent"):
            if not isinstance(data.get(name), str) or not data[name].strip():
                raise ValueError(f"{name} must be a non-empty string")
        tool_names = _string_list(data.get("tool_names"), "tool_names")
        if len(set(tool_names)) != len(tool_names) \
                or any(not _TOOL_NAME.fullmatch(name) for name in tool_names):
            raise ValueError("tool_names are invalid")
        if not isinstance(data.get("read_only"), bool):
            raise ValueError("read_only must be a boolean")
        return cls(
            skill_id=skill_id,
            title=data["title"].strip(),
            intent=data["intent"].strip(),
            triggers=_string_list(data.get("triggers"), "triggers"),
            instructions=_string_list(data.get("instructions"), "instructions"),
            tool_names=tool_names,
            provenance=_string_list(data.get("provenance"), "provenance"),
            read_only=data["read_only"],
            artifact_path=artifact_uri,
            artifact_sha256=hashlib.sha256(raw).hexdigest(),
        )

    @property
    def artifact_uri(self) -> str:
        """Logical artifact identity; ``artifact_path`` remains as a compatibility name."""
        return self.artifact_path

    def candidate_card(self, *, loaded: bool = False) -> dict:
        return {
            "skill_id": self.skill_id,
            "title": self.title,
            "intent": self.intent,
            "read_only": self.read_only,
            "tools": list(self.tool_names),
            "loaded": loaded,
        }

    def loaded_card(self) -> dict:
        return {
            **self.candidate_card(loaded=True),
            "instructions": list(self.instructions),
            "tools": list(self.tool_names),
            "provenance": list(self.provenance),
        }


@dataclass(frozen=True, slots=True)
class BusinessSkillSelection:
    """Bounded result of matching user intent against one installed catalog."""

    matcher_version: str
    skills: tuple[BusinessSkill, ...]
    trigger_matches: tuple[str, ...]
    fallback_used: bool

    @property
    def candidate_ids(self) -> tuple[str, ...]:
        return tuple(skill.skill_id for skill in self.skills)


class BusinessToolRuntime:
    """Validate a Skill catalog and dispatch only tools from loaded relevant Skills."""

    def __init__(self, skills: tuple[BusinessSkill, ...], executor: BusinessToolExecutor,
                 *, application_id: str = "",
                 required_scopes: dict[str, str] | None = None,
                 mutation_journal: MutationJournal | None = None,
                 max_pending_mutations: int = 1_000):
        skill_ids = [skill.skill_id for skill in skills]
        if len(skill_ids) != len(set(skill_ids)):
            raise ValueError("Business Skill IDs must be unique")
        schemas: dict[str, dict] = {}
        for schema in executor.tool_schemas():
            if not isinstance(schema, dict) or schema.get("type") != "function":
                raise ValueError("business tool schema must have type=function")
            function = schema.get("function")
            name = function.get("name") if isinstance(function, dict) else None
            if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
                raise ValueError("business tool name is invalid")
            if name in schemas:
                raise ValueError("business tool names must be unique")
            if not isinstance(function.get("parameters"), dict):
                raise ValueError("business tool schema requires parameters")
            schemas[name] = schema
        declared = {name for skill in skills for name in skill.tool_names}
        if declared != set(schemas):
            raise ValueError("Business Skills and executor schemas must declare the same tools")
        effects = executor.tool_effects()
        if (not isinstance(effects, dict) or set(effects) != set(schemas)
                or any(value not in {"read", "write"} for value in effects.values())):
            raise ValueError("executor must declare one read/write effect for every tool")
        for skill in skills:
            if skill.read_only and any(effects[name] == "write" for name in skill.tool_names):
                raise ValueError("read-only Business Skills may not declare write tools")
        callable_tools = set(schemas) | set(BROWSER_ACTIONS) | {"finish", "load_skill"}
        for skill in skills:
            references = {
                match.group(1) for instruction in skill.instructions
                for match in _EXPLICIT_TOOL_REFERENCE.finditer(instruction)
            }
            unavailable = references - callable_tools
            if unavailable:
                raise ValueError(
                    f"Business Skill {skill.skill_id} instructions reference unavailable "
                    f"tools: {sorted(unavailable)}")
            foreign_business = (references & set(schemas)) - set(skill.tool_names)
            if foreign_business:
                raise ValueError(
                    f"Business Skill {skill.skill_id} instructions reference business tools "
                    f"it does not declare: {sorted(foreign_business)}")
        if application_id and not re.fullmatch(r"[a-z][a-z0-9.-]{2,79}", application_id):
            raise ValueError("business tool application_id is invalid")
        scopes = dict(required_scopes or {})
        if scopes and set(scopes) != set(schemas):
            raise ValueError("required_scopes must declare every business tool")
        if any(not isinstance(scope, str) or not re.fullmatch(
                r"[a-z][a-z0-9:._-]{1,99}", scope) for scope in scopes.values()):
            raise ValueError("business tool required scope is invalid")
        if (isinstance(max_pending_mutations, bool)
                or not isinstance(max_pending_mutations, int)
                or not 1 <= max_pending_mutations <= 100_000):
            raise ValueError("max_pending_mutations must be an integer from 1 to 100000")
        self._skills = {skill.skill_id: skill for skill in skills}
        self._schemas = schemas
        self._effects = dict(effects)
        self._executor = executor
        self.application_id = application_id
        self._required_scopes = scopes
        self._mutation_journal = (mutation_journal
                                  if mutation_journal is not None
                                  else MemoryMutationJournal())
        if any(not callable(getattr(self._mutation_journal, method, None))
               for method in ("register", "claim", "finish", "get")):
            raise TypeError("mutation_journal does not implement the journal contract")
        self._max_pending_mutations = max_pending_mutations
        self._mutation_lock = threading.RLock()
        self._prepared_mutations: dict[
            str, tuple[BusinessMutationProposal, int]] = {}

    @classmethod
    def from_paths(cls, paths: list[str | Path] | tuple[str | Path, ...],
                   executor: BusinessToolExecutor) -> "BusinessToolRuntime":
        return cls(tuple(BusinessSkill.load(path) for path in paths), executor)

    @property
    def tool_names(self) -> frozenset[str]:
        return frozenset(self._schemas)

    @property
    def catalog_manifest(self) -> dict:
        """Return a versioned, non-secret description of installed Skills and tools."""
        skills = [{
            "skill_id": skill.skill_id,
            "artifact_sha256": skill.artifact_sha256,
            "read_only": skill.read_only,
            "tools": list(skill.tool_names),
        } for skill in sorted(self._skills.values(), key=lambda item: item.skill_id)]
        tools = [{
            "name": name,
            "effect": self._effects[name],
            "required_scope": self._required_scopes.get(name, ""),
            "schema_sha256": hashlib.sha256(json.dumps(
                self._schemas[name], ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            ).encode("utf-8")).hexdigest(),
        } for name in sorted(self._schemas)]
        return {"schema_version": 1, "skills": skills, "tools": tools}

    @property
    def catalog_sha256(self) -> str:
        encoded = json.dumps(
            self.catalog_manifest, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def relevant(self, task: str, limit: int = 3) -> tuple[BusinessSkill, ...]:
        folded = task.casefold()
        ranked: list[tuple[int, str, BusinessSkill]] = []
        for skill in self._skills.values():
            score = sum(1 for trigger in skill.triggers
                        if _trigger_matches(trigger, folded))
            if score:
                ranked.append((-score, skill.skill_id, skill))
        selected = [row[2] for row in sorted(ranked)]
        # A mutation Skill often contains the read tools needed for duplicate checks and
        # verification.  In that case a broader read-only Skill adds a second, ambiguous
        # route to a strict subset of the same tools.  Remove only that redundant candidate;
        # unrelated read Skills remain available for mixed-intent tasks.
        mutation_toolsets = [set(skill.tool_names) for skill in selected
                             if not skill.read_only]
        if mutation_toolsets:
            selected = [
                skill for skill in selected
                if (not skill.read_only or not any(
                    set(skill.tool_names) < tools for tools in mutation_toolsets))
            ]
        return tuple(selected[:limit])

    def select(self, task: str, *, allow_mutations: bool,
               limit: int = 5) -> BusinessSkillSelection:
        """Select task candidates and use a bounded capability-aware empty fallback.

        Trigger matching is the primary high-precision route. If it finds nothing eligible,
        expose read-only Skills plus mutation Skills only when this turn has a real approval
        path. This preserves the user-text-only injection boundary while preventing one missed
        paraphrase from making the application's entire typed tool surface disappear.
        """
        if not isinstance(allow_mutations, bool):
            raise ValueError("allow_mutations must be a boolean")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("Skill candidate limit must be an integer from 1 to 20")
        matched = self.relevant(task, limit=max(limit, len(self._skills)))
        eligible = tuple(
            skill for skill in matched if skill.read_only or allow_mutations)
        if eligible:
            return BusinessSkillSelection(
                BUSINESS_SKILL_MATCHER_VERSION, eligible[:limit],
                tuple(skill.skill_id for skill in matched), False)

        fallback = [
            skill for skill in sorted(self._skills.values(), key=lambda item: item.skill_id)
            if skill.read_only or allow_mutations
        ]
        mutation_toolsets = [set(skill.tool_names) for skill in fallback
                             if not skill.read_only]
        if mutation_toolsets:
            fallback = [
                skill for skill in fallback
                if (not skill.read_only or not any(
                    set(skill.tool_names) < tools for tools in mutation_toolsets))
            ]
        return BusinessSkillSelection(
            BUSINESS_SKILL_MATCHER_VERSION, tuple(fallback[:limit]),
            tuple(skill.skill_id for skill in matched), bool(fallback))

    def load(self, skill_id: object, allowed_ids: set[str]) -> tuple[BusinessSkill | None, str]:
        if not isinstance(skill_id, str) or skill_id not in allowed_ids:
            return None, "skill_id was not selected for this task"
        skill = self._skills.get(skill_id)
        if skill is None:
            return None, "skill_id is no longer in the server catalog"
        return skill, ""

    def schemas_for(self, loaded_ids: set[str]) -> list[dict]:
        names = {name for skill_id in loaded_ids
                 for name in self._skills[skill_id].tool_names}
        return [self._schemas[name] for name in sorted(names)]

    def is_mutation(self, name: str) -> bool:
        return self._effects.get(name) == "write"

    def _forget_mutation(self, digest: str) -> None:
        with self._mutation_lock:
            self._prepared_mutations.pop(digest, None)

    def _validate_call(self, name: str, args: object,
                       loaded_ids: set[str],
                       context: OperationContext | None = None) -> dict:
        if name not in self._schemas:
            raise BusinessToolRequestError("unknown business tool")
        allowed = {tool for skill_id in loaded_ids
                   for tool in self._skills[skill_id].tool_names}
        if name not in allowed:
            raise BusinessToolRequestError(
                "load the matching Business Skill before calling this tool")
        if not isinstance(args, dict):
            raise BusinessToolRequestError("business tool arguments must be an object")
        if context is not None:
            if not isinstance(context, OperationContext):
                raise BusinessToolIdentityError(
                    "business tool operation context is invalid")
            try:
                context.require_live(
                    application_id=self.application_id or None,
                    required_scope=self._required_scopes.get(name),
                )
            except PrincipalError as exc:
                raise BusinessToolIdentityError(str(exc)) from exc
        return args

    def execute(self, name: str, args: object,
                loaded_ids: set[str], *,
                context: OperationContext | None = None) -> BusinessToolResult:
        args = self._validate_call(name, args, loaded_ids, context)
        if self.is_mutation(name):
            raise BusinessToolRequestError(
                "business mutation requires explicit approval")
        try:
            result = (self._executor.execute(name, args)
                      if context is None
                      else self._executor.execute(name, args, context=context))
        except BusinessToolError:
            raise
        except Exception as exc:
            raise BusinessToolUnavailableError(
                f"business tool failed: {type(exc).__name__}") from exc
        if not isinstance(result, BusinessToolResult):
            raise BusinessToolContractError(
                "business tool returned an invalid result")
        return result

    def prepare_mutation(self, name: str, args: object,
                         loaded_ids: set[str], *,
                         context: OperationContext | None = None
                         ) -> BusinessMutationProposal:
        args = self._validate_call(name, args, loaded_ids, context)
        if not self.is_mutation(name):
            raise BusinessToolRequestError("business tool is read-only")
        try:
            spec = (self._executor.prepare_mutation(name, args)
                    if context is None
                    else self._executor.prepare_mutation(
                        name, args, context=context))
        except BusinessToolError:
            raise
        except Exception as exc:
            raise BusinessToolUnavailableError(
                f"business mutation preparation failed: {type(exc).__name__}") from exc
        if not isinstance(spec, BusinessMutationSpec):
            raise BusinessToolContractError(
                "business mutation preparation returned an invalid result")
        now_ms = int(time.time() * 1000)
        proposal = BusinessMutationProposal(
            tool=name, args=spec.args, effect=spec.effect, targets=spec.targets,
            nonce=uuid.uuid4().hex,
            operation_context_digest=(context.binding_digest if context else ""))
        expires_at = context.expires_at if context is not None else now_ms + 10 * 60 * 1000
        with self._mutation_lock:
            expired = [digest for digest, (_proposal, item_expires_at)
                       in self._prepared_mutations.items()
                       if item_expires_at <= now_ms]
            for digest in expired:
                self._prepared_mutations.pop(digest, None)
            if len(self._prepared_mutations) >= self._max_pending_mutations:
                raise BusinessToolError(
                    "too many business mutations are awaiting a decision")
            try:
                self._mutation_journal.register(
                    proposal.content_digest, proposal.operation_context_digest,
                    expires_at=expires_at, now_ms=now_ms)
            except MutationJournalError as exc:
                raise BusinessToolError(str(exc)) from exc
            self._prepared_mutations[proposal.content_digest] = (proposal, expires_at)
        return proposal

    def execute_mutation(self, proposal: BusinessMutationProposal,
                         approved_digest: str,
                         loaded_ids: set[str], *,
                         context: OperationContext | None = None) -> BusinessToolResult:
        if not isinstance(proposal, BusinessMutationProposal):
            raise BusinessToolError("business mutation proposal is invalid")
        digest = proposal.content_digest
        with self._mutation_lock:
            prepared_entry = self._prepared_mutations.get(digest)
            prepared = prepared_entry[0] if prepared_entry is not None else None
            if prepared != proposal or approved_digest != digest:
                prior = self._mutation_journal.get(digest)
                if prepared is None and prior is not None and prior.state != "prepared":
                    raise BusinessToolError(
                        f"business mutation proposal is already {prior.state}")
                raise BusinessToolError(
                    "business mutation approval does not match the proposal")
            self._validate_call(proposal.tool, proposal.args, loaded_ids, context)
            context_digest = context.binding_digest if context is not None else ""
            if proposal.operation_context_digest != context_digest:
                raise BusinessToolError(
                    "business mutation belongs to another operation context")
            if not self.is_mutation(proposal.tool):
                raise BusinessToolError(
                    "business mutation proposal names a read-only tool")
            # Claim atomically before dispatch. A competing caller, process restart, timeout,
            # or malformed acknowledgement cannot make the same proposal eligible again.
            try:
                self._mutation_journal.claim(
                    digest, proposal.operation_context_digest)
            except MutationJournalError as exc:
                raise BusinessToolError(str(exc)) from exc
        try:
            result = (self._executor.execute_mutation(proposal.tool, proposal.args)
                      if context is None
                      else self._executor.execute_mutation(
                          proposal.tool, proposal.args, context=context))
        except BusinessToolError:
            try:
                self._mutation_journal.finish(digest, "failed")
            except MutationJournalError:
                pass
            self._forget_mutation(digest)
            raise
        except BusinessMutationIndeterminate:
            try:
                self._mutation_journal.finish(digest, "indeterminate")
            except MutationJournalError:
                pass
            self._forget_mutation(digest)
            raise
        except Exception as exc:
            try:
                self._mutation_journal.finish(digest, "indeterminate")
            except MutationJournalError:
                pass
            self._forget_mutation(digest)
            raise BusinessMutationIndeterminate(
                f"business mutation outcome is unknown: {type(exc).__name__}") from exc
        if not isinstance(result, BusinessToolResult):
            try:
                self._mutation_journal.finish(digest, "indeterminate")
            except MutationJournalError:
                pass
            self._forget_mutation(digest)
            raise BusinessMutationIndeterminate(
                "business mutation returned an invalid acknowledgement")
        try:
            self._mutation_journal.finish(
                digest, "completed", result_digest=result.content_digest)
        except MutationJournalError as exc:
            self._forget_mutation(digest)
            raise BusinessMutationIndeterminate(
                "business mutation completed but its durable outcome was not recorded") from exc
        self._forget_mutation(digest)
        return result
