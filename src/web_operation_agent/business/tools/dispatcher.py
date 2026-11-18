"""Validated application tool catalog and fail-closed tool dispatch."""
from __future__ import annotations

import hashlib
import inspect
import json
import re
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Callable

from ...schemas import BROWSER_ACTIONS
from ...state.identity import OperationContext, PrincipalError
from ..errors import (BusinessMutationBlockedError, BusinessMutationIndeterminate,
                      BusinessToolContractError, BusinessToolDispatchError,
                      BusinessToolError, BusinessToolIdentityError,
                      BusinessToolPermissionError, BusinessToolRequestError,
                      BusinessToolUnavailableError)
from ..mutation import (MemoryMutationJournal, MutationClaimError, MutationJournal,
                        MutationJournalDurability, MutationJournalError)
from .catalog import BusinessSkillCatalog
from .contracts import (_DATA_CLASS, _TOOL_NAME, BusinessDataClassifier,
                        BusinessMutationProposal, BusinessMutationSpec,
                        BusinessPostconditionVerifier, BusinessRouteValidator,
                        BusinessToolExecutor, BusinessToolResult, ForbiddenOperation,
                        MutationPostcondition, ToolOperation)
from .skills import BusinessSkill, BusinessSkillSelection, _answer_labels


_EXPLICIT_TOOL_REFERENCE = re.compile(
    r"\bcall\s+`?([a-z][a-z0-9_]{1,63})`?", re.I)


class BusinessToolRuntime:
    """Register application tools independently of optional business playbooks."""

    def __init__(self, skills: tuple[BusinessSkill, ...], executor: BusinessToolExecutor,
                 *, application_id: str = "",
                 required_scopes: dict[str, str] | None = None,
                 mutation_journal: MutationJournal | None = None,
                 data_classifier: BusinessDataClassifier | None = None,
                 postcondition_verifier: BusinessPostconditionVerifier | None = None,
                 route_validator: BusinessRouteValidator | None = None,
                 route_skill_hints: tuple[tuple[str, str], ...] = (),
                 max_pending_mutations: int = 1_000,
                 withheld_skills: tuple[BusinessSkill, ...] = (),
                 operations: dict[str, ToolOperation] | None = None,
                 forbidden_operations: tuple[ForbiddenOperation, ...] = (),
                 answer_labels: dict[str, dict[str, str]] | None = None,
                 numeric_goal_binding: bool = False,
                 skill_registry=None, require_skill_for_writes: bool = False,
                 mutation_dispatch_enabled: bool = True):
        if type(mutation_dispatch_enabled) is not bool:
            raise TypeError("mutation_dispatch_enabled must be boolean")
        self._mutation_dispatch_enabled = mutation_dispatch_enabled
        from ...skills import SkillRegistry
        if not isinstance(require_skill_for_writes, bool):
            raise TypeError("require_skill_for_writes must be boolean")
        if skill_registry is not None and not isinstance(skill_registry, SkillRegistry):
            raise TypeError("skill_registry must be an installed SkillRegistry")
        if skill_registry is not None:
            if skills:
                raise ValueError("use one Skill registry per runtime")
            skills = skill_registry.artifacts
        self.skill_registry = skill_registry
        self.require_skill_for_writes = require_skill_for_writes
        if not isinstance(numeric_goal_binding, bool):
            raise TypeError("numeric_goal_binding must be a boolean")
        self.numeric_goal_binding = numeric_goal_binding
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
        if skill_registry is None and not declared <= set(schemas):
            raise ValueError("Business Skills reference tools not registered by the executor")
        effects = executor.tool_effects()
        if (not isinstance(effects, dict) or set(effects) != set(schemas)
                or any(value not in {"read", "write"} for value in effects.values())):
            raise ValueError("executor must declare one read/write effect for every tool")
        for skill in skills:
            if skill.read_only and any(effects.get(name) == "write" for name in skill.tool_names):
                raise ValueError("read-only Business Skills may not declare write tools")
        callable_tools = (set(schemas) | set(BROWSER_ACTIONS)
                          | {"finish", "load_skill", "describe_form", "prepare_form",
                             "prefill_form"})
        for skill in skills if skill_registry is None else ():
            references = {
                match.group(1) for instruction in skill.instructions
                for match in _EXPLICIT_TOOL_REFERENCE.finditer(instruction)
            }
            unavailable = references - callable_tools
            if unavailable:
                raise ValueError(
                    f"Business Skill {skill.skill_id} instructions reference unavailable "
                    f"tools: {sorted(unavailable)}")
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
        #: Capabilities this application installs and this session may not use -- an adapter
        #: that builds its catalog per grant (Odoo drops the write Skills for a reader) would
        #: otherwise make a capability gap say "this deployment cannot", when the truth is
        #: "you were not granted it". Searched for wording only; nothing here is loadable.
        self._withheld_skills = tuple(withheld_skills)
        if data_classifier is not None:
            if (not callable(getattr(data_classifier, "classify", None))
                    or getattr(data_classifier, "tool_names", None) != frozenset(schemas)):
                raise TypeError(
                    "data_classifier must classify every business tool")
        write_tools = frozenset(
            name for name, effect in effects.items() if effect == "write")
        if postcondition_verifier is not None:
            if (not callable(getattr(postcondition_verifier, "verify", None))
                    or getattr(postcondition_verifier, "supported_tools", None) != write_tools):
                raise TypeError(
                    "postcondition_verifier must verify every mutation tool")
        self._operations = self._validated_operations(
            operations, forbidden_operations, schemas, effects)
        self._forbidden_operations = tuple(forbidden_operations)
        self._data_classifier = data_classifier
        self._postcondition_verifier = postcondition_verifier
        if route_validator is not None and not isinstance(
                route_validator, BusinessRouteValidator):
            raise TypeError("route_validator must provide validate_tool_route")
        self._route_validator = route_validator
        for skill in self._skills.values():
            if not skill.routes:
                continue
            if route_validator is None:
                raise ValueError(
                    f"Skill {skill.skill_id} declares routes but the runtime has no route "
                    "validator to check them")
            if not skill.read_only:
                raise ValueError("only a read-only Skill may declare routes")
            for route in skill.routes:
                try:
                    route_validator.validate_tool_route(route)
                except Exception as exc:
                    raise ValueError(
                        f"Skill {skill.skill_id} declares a route outside the application's "
                        "route contract") from exc
        compiled_hints = []
        for pattern, skill_id in route_skill_hints:
            if (not isinstance(pattern, str) or not pattern.startswith("^")
                    or not pattern.endswith("$") or skill_id not in self._skills):
                raise ValueError("route Skill hints require anchored patterns and known Skills")
            if not self._skills[skill_id].read_only:
                raise ValueError("route Skill hints may name only read-only Skills")
            try:
                compiled_hints.append((re.compile(pattern), pattern, skill_id))
            except re.error as exc:
                raise ValueError("route Skill hint pattern is invalid") from exc
        self._route_skill_hints = tuple(compiled_hints)
        self._catalog = BusinessSkillCatalog(
            skills=self._skills,
            schemas=self._schemas,
            effects=self._effects,
            required_scopes=scopes,
            route_skill_hints=self._route_skill_hints,
            withheld_skills=self._withheld_skills,
        )
        self.application_id = application_id
        self.answer_labels = _answer_labels(answer_labels or {})
        self._required_scopes = scopes
        self._mutation_journal = (mutation_journal
                                  if mutation_journal is not None
                                  else MemoryMutationJournal())
        if any(not callable(getattr(self._mutation_journal, method, None))
               for method in ("register", "claim", "finish", "get")):
            raise TypeError("mutation_journal does not implement the journal contract")
        durability = getattr(self._mutation_journal, "durability", None)
        if not isinstance(durability, MutationJournalDurability):
            raise TypeError(
                "mutation_journal must declare a MutationJournalDurability")
        self._mutation_journal_durability = durability
        self._max_pending_mutations = max_pending_mutations
        self._mutation_lock = threading.RLock()
        self._prepared_mutations: dict[
            str, tuple[BusinessMutationProposal, int]] = {}

    @property
    def mutation_journal(self) -> MutationJournal:
        """The mutation journal backing prepare/claim/finish and reconciliation."""
        return self._mutation_journal

    @classmethod
    def from_registry(cls, registry, *, skill_registry=None, **kwargs):
        from .registry import ToolRegistry
        if not isinstance(registry, ToolRegistry):
            raise TypeError("registry must contain installed tool definitions")
        return cls((), registry, skill_registry=skill_registry,
                   required_scopes={t.name: t.scope for t in registry.definitions},
                   operations={t.name: t.operation for t in registry.definitions}, **kwargs)

    @classmethod
    def from_paths(cls, paths: list[str | Path] | tuple[str | Path, ...],
                   executor: BusinessToolExecutor) -> "BusinessToolRuntime":
        return cls(tuple(BusinessSkill.load(path) for path in paths), executor)

    @property
    def tool_names(self) -> frozenset[str]:
        return frozenset(self._schemas)

    @property
    def has_mutations(self) -> bool:
        return "write" in self._effects.values()

    @property
    def mutation_tool_names(self) -> frozenset[str]:
        return frozenset(
            name for name, effect in self._effects.items() if effect == "write")

    @property
    def read_tool_names(self) -> frozenset[str]:
        return frozenset(
            name for name, effect in self._effects.items() if effect == "read")

    @property
    def required_scopes(self) -> dict[str, str]:
        return dict(self._required_scopes)

    @property
    def mutation_journal_durability(self) -> MutationJournalDurability:
        return self._mutation_journal_durability

    @property
    def data_classifier(self) -> BusinessDataClassifier | None:
        return self._data_classifier

    @property
    def postcondition_verifier(self) -> BusinessPostconditionVerifier | None:
        return self._postcondition_verifier

    @property
    def route_validator(self) -> BusinessRouteValidator | None:
        return self._route_validator

    @property
    def skills(self) -> tuple[BusinessSkill, ...]:
        """Every Skill this runtime can load, in catalog order."""
        return tuple(self._skills.values())

    @staticmethod
    def _validated_operations(operations, forbidden, schemas, effects) -> dict:
        """Classify every tool by entity and operation, and refuse a forbidden capability.

        The check is here rather than at dispatch because a capability this deployment
        forbids should not exist in a session at all: a Skill that declares one refuses the
        catalog, with the rule and the tool named, instead of failing the one turn that
        happens to call it.
        """
        if not isinstance(forbidden, tuple) or any(
                not isinstance(rule, ForbiddenOperation) for rule in forbidden):
            raise TypeError("forbidden_operations must be ForbiddenOperation values")
        if operations is None:
            if forbidden:
                raise ValueError(
                    "forbidden operations require an entity and operation for every tool")
            return {}
        if (not isinstance(operations, dict)
                or any(not isinstance(value, ToolOperation) for value in operations.values())):
            raise TypeError("operations must map every tool to a ToolOperation")
        if set(operations) != set(schemas):
            raise ValueError("operations must classify every business tool")
        for name, operation in operations.items():
            if operation.reads_only != (effects[name] == "read"):
                raise ValueError(
                    f"business tool {name} is declared {effects[name]} but classified "
                    f"{operation.operation}")
            for rule in forbidden:
                if rule.matches(operation):
                    raise ValueError(
                        f"business tool {name} performs {rule}, which this deployment "
                        "forbids")
        return dict(operations)

    @property
    def operations(self) -> dict:
        """What each tool does, by entity and operation; empty when none was declared."""
        return dict(self._operations)

    def _classify_result(self, name: str, result: BusinessToolResult) -> BusinessToolResult:
        if self._route_validator is not None:
            try:
                for route in result.allowed_routes:
                    self._route_validator.validate_tool_route(route)
            except Exception as exc:
                raise BusinessToolContractError(
                    "application returned a route outside its adapter contract") from exc
        operation = self._operations.get(name)
        if operation is not None:
            # The server's own words for what this call did, beside whatever the
            # application chose to report about itself.
            result = replace(result, audit={**result.audit,
                                            "server_entity": operation.entity,
                                            "server_operation": operation.operation})
        if self._data_classifier is None:
            return result
        try:
            classes = self._data_classifier.classify(name, result)
        except BusinessToolError:
            raise
        except Exception as exc:
            raise BusinessToolContractError(
                "application data classification failed") from exc
        if (not isinstance(classes, tuple)
                or len(classes) != len(set(classes))
                or any(not isinstance(item, str) or not _DATA_CLASS.fullmatch(item)
                       for item in classes)):
            raise BusinessToolContractError(
                "application data classifier returned invalid classes")
        return replace(result, data_classes=classes)

    def time_zone(self) -> str:
        """The zone the application says its signed-in user is in, as an IANA name.

        The host passes it to the turn so relative dates mean the person's days. An adapter
        that does not know one answers "", and the turn then says nothing about the clock
        rather than claiming UTC is where they are.
        """
        reader = getattr(self._executor, "time_zone", None)
        if not callable(reader):
            return ""
        try:
            zone = reader()
        except Exception:  # an adapter fault must not stop the turn before it starts
            return ""
        return zone if isinstance(zone, str) and len(zone) <= 64 else ""

    @property
    def catalog_manifest(self) -> dict:
        return self._catalog.catalog_manifest

    @property
    def catalog_sha256(self) -> str:
        return self._catalog.catalog_sha256

    def resolve_capability(self, goal: str, *, allow_mutations: bool,
                           effect: str = "write",
                           extra: tuple[str, ...] = (),
                           actor_scopes: tuple[str, ...] | None = None) -> tuple[str, ...]:
        return self._catalog.resolve_capability(
            goal, allow_mutations=allow_mutations, effect=effect, extra=extra,
            actor_scopes=actor_scopes)

    def withheld_capability(self, goal: str) -> tuple[str, ...]:
        return self._catalog.withheld_capability(goal)

    def relevant(self, task: str, limit: int = 3) -> tuple[BusinessSkill, ...]:
        return self._catalog.relevant(task, limit=limit)

    def capability_skills(self, *, allow_mutations: bool) -> tuple[BusinessSkill, ...]:
        return self._catalog.capability_skills(allow_mutations=allow_mutations)

    def select(self, task: str, *, allow_mutations: bool,
               trusted_entry_path: str = "", limit: int = 5) -> BusinessSkillSelection:
        return self._catalog.select(
            task,
            allow_mutations=allow_mutations,
            trusted_entry_path=trusted_entry_path,
            limit=limit,
        )

    def load(self, skill_id: object,
             allowed_ids: set[str]) -> tuple[BusinessSkill | None, str]:
        return self._catalog.load(skill_id, allowed_ids)

    def requested_writes(self, task: str) -> frozenset[str]:
        return self._catalog.requested_writes(task)

    def available_schemas(self, *, allow_mutations: bool,
                          actor_scopes: tuple[str, ...] | None = None,
                          writes: frozenset[str] | None = None,
                          loaded_ids: set[str] | None = None) -> list[dict]:
        """Offer deployed tools allowed by this turn, never by loaded Skill IDs.

        `writes`, when given, is the write tools the request asks to use; no other write
        is offered. Dispatch rechecks the live operation context; schema filtering is only
        a bounded discovery surface, not an authorization decision.
        """
        if not isinstance(allow_mutations, bool):
            raise TypeError("allow_mutations must be a boolean")
        scopes = None if actor_scopes is None else set(actor_scopes)
        if self.require_skill_for_writes:
            supported = {op for sid in (loaded_ids or ()) if sid in self._skills
                         for op in self._skills[sid].write_operations}
            writes = supported if writes is None else supported & writes
        return [self._schemas[name] for name in sorted(self._schemas)
                if (self._effects[name] == "read"
                    or allow_mutations and (writes is None or name in writes))
                and (scopes is None or not self._required_scopes.get(name)
                     or self._required_scopes[name] in scopes)]

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
        # The installed executor owns the tool. Skill text may explain it but
        # cannot grant, withdraw, or gate its availability.
        if not isinstance(args, dict):
            raise BusinessToolRequestError("business tool arguments must be an object")
        if context is not None:
            if not isinstance(context, OperationContext):
                raise BusinessToolIdentityError(
                    "business tool operation context is invalid")
            try:
                context.require_live(application_id=self.application_id or None)
            except PrincipalError as exc:
                raise BusinessToolIdentityError(str(exc)) from exc
            # A live principal that lacks the scope is an authorization answer, not a broken
            # identity. Checked separately so the two never have to be told apart by text.
            required_scope = self._required_scopes.get(name)
            if required_scope and required_scope not in context.principal.scopes:
                raise BusinessToolPermissionError(
                    "the current actor is not permitted to use this business tool",
                    layer="tool_authorization", required_scope=required_scope)
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
        return self._classify_result(name, result)

    def prepare_mutation(self, name: str, args: object,
                         loaded_ids: set[str], *,
                         context: OperationContext | None = None,
                         skill_id: str = ""
                         ) -> BusinessMutationProposal:
        args = self._validate_call(name, args, loaded_ids, context)
        if not self.is_mutation(name):
            raise BusinessToolRequestError("business tool is read-only")
        if self.require_skill_for_writes or skill_id:
            candidates = [self._skills[s] for s in loaded_ids if s in self._skills
                          and name in getattr(self._skills[s], "write_operations", ())
                          and (not skill_id or s == skill_id)]
            if len(candidates) != 1:
                raise BusinessToolRequestError("write requires one explicit loaded supporting Skill")
            skill_id = candidates[0].skill_id
        supporting = self._skills.get(skill_id) if skill_id else None
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
            operation_context_digest=(context.binding_digest if context else ""),
            details=spec.details, skill_id=skill_id,
            skill_digest=(supporting.artifact_sha256 if supporting else ""),
            effect_projection=spec.effect_projection)
        principal = context.principal if context is not None else None
        key_payload = [self.application_id, name,
                       principal.tenant_id if principal else "",
                       principal.workspace_id if principal else "",
                       principal.actor_id if principal else "",
                       spec.reconcile_key or spec.args]
        business_key = hashlib.sha256(json.dumps(
            key_payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()
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
                    expires_at=expires_at, now_ms=now_ms, business_key=business_key,
                    reconciliation={"application_id": self.application_id, "tool": name,
                                    "args": (spec.reconciliation if spec.reconciliation is not None else spec.args),
                                    "identity": ({key: value for key, value in
                                        principal.audit_projection().items() if key in {
                                            "tenant_digest", "workspace_digest", "actor_digest"}}
                                        if principal else {})})
            except MutationJournalError as exc:
                raise BusinessToolError(str(exc)) from exc
            self._prepared_mutations[proposal.content_digest] = (proposal, expires_at)
        return proposal

    def execute_mutation(self, proposal: BusinessMutationProposal,
                         approved_digest: str,
                         loaded_ids: set[str], *,
                         context: OperationContext | None = None,
                         on_claimed: Callable[[], None] | None = None) -> BusinessToolResult:
        if not self._mutation_dispatch_enabled:
            raise BusinessMutationBlockedError("mutation dispatch is disabled for preparation",
                                               reason="preparation_only")
        if not isinstance(proposal, BusinessMutationProposal):
            raise BusinessToolError("business mutation proposal is invalid")
        digest = proposal.content_digest
        with self._mutation_lock:
            prepared_entry = self._prepared_mutations.get(digest)
            prepared = prepared_entry[0] if prepared_entry is not None else None
            if prepared != proposal or approved_digest != digest:
                prior = self._mutation_journal.get(digest)
                if prepared is None and prior is not None and prior.state != "prepared":
                    raise BusinessMutationBlockedError(
                        f"business mutation proposal is already {prior.state}",
                        reason="already_consumed", state=prior.state)
                raise BusinessToolError(
                    "business mutation approval does not match the proposal")
            self._validate_call(proposal.tool, proposal.args, loaded_ids, context)
            if self.require_skill_for_writes or proposal.skill_id:
                skill = self._skills.get(proposal.skill_id)
                if (skill is None or proposal.skill_id not in loaded_ids
                        or proposal.skill_digest != skill.artifact_sha256
                        or proposal.tool not in getattr(skill, "write_operations", ())):
                    raise BusinessToolRequestError("write Skill binding is no longer valid")
            context_digest = context.binding_digest if context is not None else ""
            if proposal.operation_context_digest != context_digest:
                raise BusinessToolError(
                    "business mutation belongs to another operation context")
            if not self.is_mutation(proposal.tool):
                raise BusinessToolError(
                    "business mutation proposal names a read-only tool")
            # Claim atomically before dispatch. A competing caller, process restart, timeout,
            # or malformed acknowledgement cannot make the same proposal eligible again.
            if on_claimed is not None and not callable(on_claimed):
                raise TypeError("on_claimed must be callable or None")
            try:
                claim_params = inspect.signature(self._mutation_journal.claim).parameters
                if "dispatch_lease_expires_at" in claim_params:
                    self._mutation_journal.claim(
                        digest, proposal.operation_context_digest,
                        dispatch_lease_expires_at=prepared_entry[1])
                else:
                    self._mutation_journal.claim(
                        digest, proposal.operation_context_digest)
            except MutationClaimError as exc:
                raise BusinessMutationBlockedError(
                    str(exc), reason=exc.reason, state=exc.state) from exc
            except MutationJournalError as exc:
                raise BusinessToolError(str(exc)) from exc
            if on_claimed is not None:
                try:
                    on_claimed()
                except Exception as exc:
                    # Nothing reached the application yet.  Release the key with a definite
                    # failed outcome rather than leaving a false in-flight write behind.
                    try:
                        self._mutation_journal.finish(digest, "failed")
                    except MutationJournalError:
                        pass
                    self._forget_mutation(digest)
                    raise BusinessToolUnavailableError(
                        "mutation dispatch audit could not be recorded") from exc
        try:
            result = (self._executor.execute_mutation(proposal.tool, proposal.args)
                      if context is None
                      else self._executor.execute_mutation(
                          proposal.tool, proposal.args, context=context))
        except BusinessToolDispatchError as exc:
            try:
                self._mutation_journal.finish(
                    digest, "indeterminate", reconciliation={"phase": "dispatch"})
            except MutationJournalError:
                pass
            self._forget_mutation(digest)
            raise BusinessMutationIndeterminate(
                "business mutation transport returned no safe outcome",
                phase="dispatch") from exc
        except BusinessToolError:
            try:
                self._mutation_journal.finish(digest, "failed")
            except MutationJournalError:
                pass
            self._forget_mutation(digest)
            raise
        except BusinessMutationIndeterminate as exc:
            try:
                self._mutation_journal.finish(
                    digest, "indeterminate", reconciliation=exc.reconciliation)
            except MutationJournalError:
                pass
            self._forget_mutation(digest)
            raise
        except Exception as exc:
            try:
                self._mutation_journal.finish(
                    digest, "indeterminate", reconciliation={"phase": "dispatch"})
            except MutationJournalError:
                pass
            self._forget_mutation(digest)
            raise BusinessMutationIndeterminate(
                f"business mutation outcome is unknown: {type(exc).__name__}",
                phase="dispatch") from exc
        if not isinstance(result, BusinessToolResult):
            try:
                self._mutation_journal.finish(digest, "indeterminate")
            except MutationJournalError:
                pass
            self._forget_mutation(digest)
            raise BusinessMutationIndeterminate(
                "business mutation returned an invalid acknowledgement")
        try:
            result = self._classify_result(proposal.tool, result)
            if proposal.effect_projection is not None:
                proposal.effect_projection.require_receipt(result.content)
            if self._postcondition_verifier is not None:
                postcondition = self._postcondition_verifier.verify(
                    proposal.tool, proposal.args, result, context=context)
                if not isinstance(postcondition, MutationPostcondition):
                    raise BusinessToolContractError(
                        "application postcondition verifier returned an invalid verdict")
                if not postcondition.verified:
                    raise BusinessMutationIndeterminate(
                        "business mutation postcondition was not verified")
                result = replace(result, audit={
                    **result.audit,
                    "postcondition": {
                        "code": postcondition.code,
                        "verified": True,
                        "evidence": postcondition.evidence,
                    },
                })
        except BusinessMutationIndeterminate as exc:
            try:
                self._mutation_journal.finish(
                    digest, "indeterminate", reconciliation=exc.reconciliation)
            except MutationJournalError:
                pass
            self._forget_mutation(digest)
            raise
        except Exception as exc:
            try:
                self._mutation_journal.finish(
                    digest, "indeterminate", reconciliation={"phase": "postcondition"})
            except MutationJournalError:
                pass
            self._forget_mutation(digest)
            raise BusinessMutationIndeterminate(
                "business mutation postcondition verification failed",
                phase="postcondition") from exc
        try:
            self._mutation_journal.finish(
                digest, "completed", result_digest=result.content_digest)
        except MutationJournalError as exc:
            self._forget_mutation(digest)
            raise BusinessMutationIndeterminate(
                "business mutation completed but its durable outcome was not recorded") from exc
        self._forget_mutation(digest)
        return result
