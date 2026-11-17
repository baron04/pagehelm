"""Installed Skills and their approved writes, as steps of a model-driven turn.

A turn that decides its own steps loads a Skill when the request asks for a change the
Skill covers, reads its knowledge, and proposes one typed operation at a time. Each
proposal runs the shared chain of `BusinessWriteService.run`: read-only preparation
under the actor, the application's projection of what it prepared compared with the
proposed inputs, one fresh visible approval, one dispatch and the application's readback.

Loading grants nothing. What may be proposed is decided by the deployment's enablement,
the actor's scopes and the packages loaded in this turn; what may run, by the person's
approval of that exact proposal. Skill text is knowledge and never a permission.
"""
from __future__ import annotations

import hashlib
import json

from ..business.effects import canonical
from ..business.errors import BusinessToolError, BusinessToolRequestError
from ..business.tools.registry import validate_input
from ..business.write_service import BusinessWriteService, OperationOutcome
from ..execution.value_source import append_overwrites_existing
from ..skills import SkillContext
from .skill_literals import _existing_append_supplied, literal_supplied, require_literal_inputs


class SkillWrites:
    """One turn's loaded Skills and the operations they let it propose."""

    def __init__(self, application, approvals):
        self.runtime = application.business_tools
        self.operations = application.ports.operations
        self.approvals = approvals
        self.context = SkillContext(self.operations.skills)
        #: Shared with the write service: the runtime binds every write to one of these.
        self.loaded_ids: set[str] = set()
        self.service = BusinessWriteService(self.runtime, approvals, self.loaded_ids)
        #: Proposals this turn completed, so that the same one is not asked for twice.
        self._completed: set[str] = set()

    def _offered(self, skill_ids) -> dict:
        """The write definitions these Skills support that this deployment and actor may use."""
        principal = self.approvals.principal
        names = {schema["function"]["name"] for schema in self.runtime.available_schemas(
            allow_mutations=self.approvals.mutations_enabled,
            actor_scopes=principal.scopes if principal is not None else (),
            loaded_ids=set(skill_ids))}
        supported = {name for skill in self.operations.skills.artifacts
                     if skill.skill_id in skill_ids for name in skill.write_operations}
        return {d.name: d for d in self.operations.tools.definitions
                if d.effect == "write" and d.name in names and d.name in supported}

    def catalog(self) -> tuple:
        """(Skill, its offered definitions) for every installed Skill that offers a write."""
        found = []
        for skill in self.operations.skills.artifacts:
            offered = self._offered({skill.skill_id})
            definitions = tuple(offered[name] for name in skill.write_operations if name in offered)
            if definitions:
                found.append((skill, definitions))
        return tuple(found)

    def load(self, skill_id: str, *, step: int):
        """Load one installed Skill into this turn; raises ValueError when it offers nothing."""
        if not isinstance(skill_id, str) or not self._offered({skill_id}):
            raise ValueError("no installed Skill of this identity offers an operation here")
        skill = self.context.load(skill_id)
        if skill_id not in self.loaded_ids:
            self.loaded_ids.add(skill_id)
            self.approvals.ledger.append("business_skill_loaded", step=step, skill_id=skill_id,
                                         skill_sha256=skill.artifact_sha256, side_effect_possible=False)
        return skill

    def resource(self, skill_id: str, path: str, *, step: int) -> str:
        text = self.context.read_resource(skill_id, path)
        skill = self.context.get_loaded(skill_id)
        self.approvals.ledger.append(
            "business_skill_resource_loaded", step=step, skill_id=skill_id,
            skill_sha256=skill.artifact_sha256, resource=path,
            resource_sha256=hashlib.sha256(text.encode()).hexdigest(), side_effect_possible=False)
        return text

    def definitions(self) -> tuple:
        """The operations the Skills loaded so far let this turn propose."""
        return tuple(self._offered(self.loaded_ids).values())

    @staticmethod
    def _validate_prepared_update(definition, inputs: dict, texts, proposal) -> None:
        if definition.operation.operation != "update" or proposal.effect_projection is None:
            return
        before_map = json.loads(proposal.effect_projection.before_json)
        if not before_map:
            return
        changed_values = proposal.effect_projection.change.values
        prompt = texts[-1] if texts else ""
        field_map = {path: target_field for path, _entity, target_field in definition.input_fields}
        for path in definition.literal_inputs:
            target = inputs
            keys = path.split(".")
            for key in keys[:-1]:
                target = target.get(key) if isinstance(target, dict) else None
            if not isinstance(target, dict) or keys[-1] not in target:
                continue
            value = target[keys[-1]]
            if not isinstance(value, str) or not value:
                continue
            app_field = field_map.get(path) or field_map.get(keys[-1]) or keys[-1]
            if not literal_supplied(value, texts):
                old_val = before_map.get(app_field, before_map.get(keys[-1]))
                allowed = (old_val,) if isinstance(old_val, str) else ()
                if not _existing_append_supplied(value, texts, allowed):
                    raise BusinessToolRequestError(
                        f"input.{path} must copy a value supplied by the person in full, preserving case"
                    )
        for app_field, new_val in changed_values.items():
            old_val = before_map.get(app_field)
            if append_overwrites_existing(prompt, app_field, old_val, new_val):
                raise BusinessToolRequestError(
                    f"用户要求在 {app_field} 中追加或补充内容，当前修改覆盖了原有内容；"
                    f"请先读取并保留目标记录该字段原值，再在其后追加本次内容。"
                )

    def propose(self, operation: str, inputs: dict, *, step: int, texts,
                prefill_port=None, prefill_observation=None,
                existing_values: dict[str, tuple[str, ...]] | None = None) -> OperationOutcome:
        """One proposed operation, to its verified result or to the reason it did not run.

        `texts` are the person's own messages: a registered literal input must copy one of
        them in full (or append user-supplied text to a read existing value on update).
        The expected change is the application's own reading of the inputs, and what it
        then prepares must be that change before anyone is asked.
        """
        ledger = self.approvals.ledger
        definition = self._offered(self.loaded_ids).get(operation)
        try:
            if definition is None:
                raise BusinessToolRequestError("operation requires a loaded supporting Skill")
            if not isinstance(inputs, dict):
                raise BusinessToolRequestError("operation inputs must be an object")
            supporters = [skill.skill_id for skill in self.context.loaded
                          if operation in skill.write_operations]
            if len(supporters) != 1:
                raise BusinessToolRequestError("operation requires exactly one loaded supporting Skill")
            key = canonical([operation, inputs])
            if key in self._completed:
                return OperationOutcome("repeated")
            validate_input(json.loads(definition.input_json), inputs)
            require_literal_inputs(inputs, definition, texts, existing_values=existing_values)
            expected = (definition.effect_contract.project_inputs(inputs)
                        if definition.effect_contract is not None else None)
        except (BusinessToolError, ValueError) as exc:
            ledger.append("skill_operation_refused", step=step, tool=str(operation)[:64],
                          error_code=getattr(exc, "code", "skill_proposal"),
                          detail=str(exc)[:300], side_effect_possible=False)
            return OperationOutcome("refused", reason="agent_refused", error=exc)
        context = self.approvals.context(step, operation, hold_ms=self.approvals.approval_hold_ms)
        outcome = self.service.run(
            operation, inputs, step=step, context=context,
            skill_id=supporters[0], expected=expected,
            prefill_port=prefill_port,
            prefill_observation=prefill_observation,
            check_proposal=lambda proposal: self._validate_prepared_update(
                definition, inputs, texts, proposal
            ),
        )
        if outcome.state == "completed":
            self._completed.add(key)
            self.operations.invalidate_reads(frozenset({outcome.result.content.get("entity", "")}))
        elif outcome.error is not None and outcome.state != "indeterminate":
            ledger.append("skill_operation_refused", step=step, tool=operation,
                          error_code=getattr(outcome.error, "code", "skill_proposal"),
                          detail=str(outcome.error)[:300], asked=outcome.asked,
                          side_effect_possible=False)
        return outcome


__all__ = ["SkillWrites"]
